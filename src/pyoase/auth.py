"""Azure AD B2C authentication for the OASE cloud.

The OASE tenant exposes only the interactive ``B2C_1A_SignUp_SignIn`` policy (no ROPC
grant), so we reproduce the app's auth-code + PKCE flow headlessly by scripting the B2C
"SelfAsserted" local-account login. This mirrors the flow validated end-to-end in
``scripts/login_probe.py``.

Usage (inside Home Assistant, with the shared aiohttp session)::

    auth = OaseAuth(session, email, password)
    await auth.async_login()                 # validates credentials, gets tokens
    token = await auth.async_get_access_token()   # cached; auto-refreshes
    stored = auth.token_data                 # persist this (contains refresh_token)
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from typing import Any

import aiohttp

from .const import (
    B2C_AUTHORIZE_URL,
    B2C_CONFIRMED_URL,
    B2C_POLICY,
    B2C_SELF_ASSERTED_URL,
    B2C_TOKEN_URL,
    CLIENT_ID,
    REDIRECT_URI,
    SCOPES,
    USER_AGENT,
)
from .exceptions import OaseAuthError, OaseConnectionError, OaseResponseError

_SETTINGS_RE = re.compile(r"var SETTINGS\s*=\s*(\{.*?\});", re.DOTALL)
#: refresh a little before the token actually expires
_EXPIRY_SKEW = 60


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


class OaseAuth:
    """Manages B2C tokens for one OASE account."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str | None = None,
        password: str | None = None,
        *,
        token_data: dict[str, Any] | None = None,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._access_token: str | None = None
        self._expires_at: float = 0.0
        self._refresh_token: str | None = None
        if token_data:
            self._refresh_token = token_data.get("refresh_token")

    @property
    def token_data(self) -> dict[str, Any]:
        """Serialisable token state to persist in the HA config entry."""
        return {"refresh_token": self._refresh_token}

    async def async_get_access_token(self) -> str:
        """Return a valid access token, refreshing or re-logging in as needed."""
        if self._access_token and time.monotonic() < self._expires_at:
            return self._access_token
        if self._refresh_token:
            try:
                return await self._async_refresh()
            except OaseAuthError:
                # refresh token expired/revoked -> fall back to full login
                self._refresh_token = None
        return await self.async_login()

    async def async_login(self) -> str:
        """Perform the full scripted B2C interactive login. Returns the access token.

        The interactive dance runs on a private session whose cookie jar does NOT
        RFC-quote cookies: B2C's ``x-ms-cpim-cache|...`` cookies must be sent verbatim,
        and HA's shared session quotes them (which yields ``400 Bad Request``). The
        resulting Bearer token is then used on the shared session for API calls.
        """
        if not self._email or not self._password:
            raise OaseAuthError("email and password required for login")
        verifier, challenge = _pkce_pair()
        jar = aiohttp.CookieJar(quote_cookie=False)
        async with aiohttp.ClientSession(cookie_jar=jar) as login_session:
            trans_id, csrf = await self._async_start_authorize(login_session, challenge)
            await self._async_self_asserted(login_session, trans_id, csrf)
            code = await self._async_confirm(login_session, trans_id, csrf)
            return await self._async_exchange_code(login_session, code, verifier)

    # -- individual steps --------------------------------------------------------

    async def _async_start_authorize(
        self, session: aiohttp.ClientSession, challenge: str
    ) -> tuple[str, str]:
        params = {
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": secrets.token_hex(16),
            "nonce": secrets.token_hex(16),
            "response_mode": "query",
        }
        try:
            async with session.get(
                B2C_AUTHORIZE_URL, params=params, headers={"User-Agent": USER_AGENT}
            ) as resp:
                html = await resp.text()
        except aiohttp.ClientError as err:
            raise OaseConnectionError(f"authorize request failed: {err}") from err
        match = _SETTINGS_RE.search(html)
        if not match:
            raise OaseResponseError("B2C login page did not contain SETTINGS (flow changed?)")
        settings = json.loads(match.group(1))
        trans_id = settings.get("transId")
        csrf = settings.get("csrf")
        if not trans_id or not csrf:
            raise OaseResponseError("B2C SETTINGS missing transId/csrf")
        return trans_id, csrf

    async def _async_self_asserted(
        self, session: aiohttp.ClientSession, trans_id: str, csrf: str
    ) -> None:
        data = {"request_type": "RESPONSE", "signInName": self._email, "password": self._password}
        headers = {
            "X-CSRF-TOKEN": csrf,
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": USER_AGENT,
        }
        try:
            async with session.post(
                B2C_SELF_ASSERTED_URL,
                params={"tx": trans_id, "p": B2C_POLICY},
                data=data,
                headers=headers,
            ) as resp:
                payload = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise OaseConnectionError(f"SelfAsserted request failed: {err}") from err
        if str(payload.get("status")) != "200":
            raise OaseAuthError(f"login rejected: {payload.get('message') or payload}")

    async def _async_confirm(
        self, session: aiohttp.ClientSession, trans_id: str, csrf: str
    ) -> str:
        params = {
            "rememberMe": "false",
            "csrf_token": csrf,
            "tx": trans_id,
            "p": B2C_POLICY,
        }
        try:
            async with session.get(
                B2C_CONFIRMED_URL,
                params=params,
                headers={"User-Agent": USER_AGENT},
                allow_redirects=False,
            ) as resp:
                location = resp.headers.get("Location", "")
                body = "" if location else await resp.text()
        except aiohttp.ClientError as err:
            raise OaseConnectionError(f"confirm request failed: {err}") from err
        if location:
            from urllib.parse import parse_qs, urlparse

            code = parse_qs(urlparse(location).query).get("code", [""])[0]
        else:
            m = re.search(r'name="code"\s+value="([^"]+)"', body)
            code = m.group(1) if m else ""
        if not code:
            raise OaseAuthError("no authorization code returned by B2C")
        return code

    async def _async_exchange_code(
        self, session: aiohttp.ClientSession, code: str, verifier: str
    ) -> str:
        data = {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "scope": SCOPES,
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        }
        return await self._async_token_request(session, data)

    async def _async_refresh(self) -> str:
        # refresh is a cookieless POST; the shared session is fine here
        data = {
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "scope": SCOPES,
            "refresh_token": self._refresh_token,
        }
        return await self._async_token_request(self._session, data)

    async def _async_token_request(
        self, session: aiohttp.ClientSession, data: dict[str, str]
    ) -> str:
        try:
            async with session.post(
                B2C_TOKEN_URL, data=data, headers={"User-Agent": USER_AGENT}
            ) as resp:
                payload = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise OaseConnectionError(f"token request failed: {err}") from err
        if "access_token" not in payload:
            err_desc = payload.get("error_description", payload.get("error", "unknown"))
            # AADB2C90080/90085 etc. => invalid/expired grant
            raise OaseAuthError(f"token request failed: {err_desc}")
        self._access_token = payload["access_token"]
        self._expires_at = time.monotonic() + int(payload.get("expires_in", 3600)) - _EXPIRY_SKEW
        if payload.get("refresh_token"):
            self._refresh_token = payload["refresh_token"]
        return self._access_token
