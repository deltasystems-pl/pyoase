#!/usr/bin/env python3
"""End-to-end proof: headless Azure AD B2C login -> OASE cloud -> device state.

Reproduces the OASE Control app's OAuth2 auth-code + PKCE flow without a browser by
scripting the B2C "SelfAsserted" local-account login, then calls the API. Reads
credentials from env (never hardcode them):

    OASE_EMAIL=you@example.com OASE_PASSWORD='...' python3 scripts/login_probe.py

By default it is READ-ONLY: it lists gateways/devices and exercises the SendONetPacket
control tunnel with a GET_LIVE_SCENE packet (which reads, never switches). Pass
--set-socket N --on/--off to physically actuate a named socket.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.cookiejar
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from pyoase import onet  # noqa: E402

CLIENT_ID = "8dfe4495-b83f-4e4f-861c-83b6b3cbaa3b"  # public client id (from the app's authorize URL)
REDIRECT_URI = f"msal{CLIENT_ID}://auth"
SCOPE = (
    "https://oasecustomersprod.onmicrosoft.com/api/oase.read "
    "https://oasecustomersprod.onmicrosoft.com/api/oase.readwrite "
    "openid profile offline_access"
)
TENANT = "oasecustomersprod.onmicrosoft.com"
POLICY = "B2C_1A_SignUp_SignIn"
B2C = f"https://account.oase.com/{TENANT}/{POLICY}"
API_BASE = "https://app-oasecloud-prod.azurewebsites.net"
API_VERSION = "5.0"
UA = "Mozilla/5.0 (Linux; Android 13; SM-S911B) AppleWebKit/537.36 Chrome/125.0 Mobile Safari/537.36"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # capture 3xx instead of following (redirect is a custom scheme)


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def login(email: str, password: str) -> dict:
    cj = http.cookiejar.CookieJar()
    follow = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    nofollow = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), _NoRedirect())
    for o in (follow, nofollow):
        o.addheaders = [("User-Agent", UA)]

    verifier, challenge = _pkce()
    state = secrets.token_hex(16)
    nonce = secrets.token_hex(16)

    # 1) GET authorize -> login page carrying SETTINGS{ transId, csrf }
    q = urllib.parse.urlencode({
        "client_id": CLIENT_ID, "response_type": "code", "redirect_uri": REDIRECT_URI,
        "scope": SCOPE, "code_challenge": challenge, "code_challenge_method": "S256",
        "state": state, "nonce": nonce, "response_mode": "query",
    })
    html = follow.open(f"{B2C}/oauth2/v2.0/authorize?{q}", timeout=30).read().decode("utf-8", "replace")
    m = re.search(r'var SETTINGS\s*=\s*(\{.*?\});', html, re.DOTALL)
    m = m or re.search(r'"csrf"\s*:\s*"([^"]+)"', html)
    if not m:
        raise SystemExit("could not find B2C SETTINGS on the authorize page (login page shape changed?)")
    settings = json.loads(m.group(1))
    trans_id = settings["transId"]
    csrf = settings["csrf"]

    # 2) POST credentials to SelfAsserted
    sa_body = urllib.parse.urlencode({
        "request_type": "RESPONSE", "signInName": email, "password": password,
    }).encode()
    sa_url = f"{B2C}/SelfAsserted?" + urllib.parse.urlencode({"tx": trans_id, "p": POLICY})
    sa_req = urllib.request.Request(sa_url, data=sa_body, method="POST")
    sa_req.add_header("X-CSRF-TOKEN", csrf)
    sa_req.add_header("X-Requested-With", "XMLHttpRequest")
    sa_req.add_header("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
    sa_resp = json.loads(follow.open(sa_req, timeout=30).read().decode())
    if str(sa_resp.get("status")) != "200":
        raise SystemExit(f"SelfAsserted login rejected: {sa_resp}")

    # 3) GET confirmed -> 302 to redirect_uri?code=...
    conf_url = f"{B2C}/api/CombinedSigninAndSignup/confirmed?" + urllib.parse.urlencode({
        "rememberMe": "false", "csrf_token": csrf, "tx": trans_id, "p": POLICY,
    })
    location = ""
    body = ""
    try:
        conf = nofollow.open(conf_url, timeout=30)
        location = conf.headers.get("Location", "")
        if not location:
            body = conf.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # urllib refuses to "redirect" to the custom msal:// scheme and raises; the
        # Location (carrying ?code=...) is still on the exception.
        if e.code in (301, 302, 303, 307, 308):
            location = e.headers.get("Location", "")
        else:
            raise
    if location:
        code = urllib.parse.parse_qs(urllib.parse.urlparse(location).query).get("code", [""])[0]
    else:
        m2 = re.search(r'name="code"\s+value="([^"]+)"', body)
        code = m2.group(1) if m2 else ""
    if not code:
        raise SystemExit(f"no authorization code returned (location={location!r})")

    # 4) exchange code for tokens
    tok_body = urllib.parse.urlencode({
        "grant_type": "authorization_code", "client_id": CLIENT_ID, "scope": SCOPE,
        "code": code, "redirect_uri": REDIRECT_URI, "code_verifier": verifier,
    }).encode()
    tok = json.loads(follow.open(
        urllib.request.Request(f"{B2C}/oauth2/v2.0/token", data=tok_body, method="POST"), timeout=30
    ).read().decode())
    if "access_token" not in tok:
        raise SystemExit(f"token exchange failed: {tok}")
    return tok


def api(method: str, path: str, token: str, body: dict | None = None, query: str = "") -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{API_BASE}{path}{query}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("api-version", API_VERSION)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise SystemExit(f"HTTP {e.code} {method} {path}: {e.read().decode(errors='replace')[:400]}") from e


def send_onet(token: str, serial: str, packet: bytes) -> bytes:
    reply = api("POST", f"/Gateway/{serial}/SendONetPacket", token,
                body={"data": base64.b64encode(packet).decode()}, query="?timeout=10")
    return base64.b64decode(reply["data"]) if reply.get("data") else b""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set-socket", type=int, choices=[1, 2, 3])
    ap.add_argument("--on", action="store_true")
    ap.add_argument("--off", action="store_true")
    args = ap.parse_args()

    email = os.environ.get("OASE_EMAIL")
    password = os.environ.get("OASE_PASSWORD")
    if not email or not password:
        raise SystemExit("set OASE_EMAIL and OASE_PASSWORD")

    print("1) headless B2C login ...")
    tok = login(email, password)
    at = tok["access_token"]
    print(f"   OK — access_token {at[:12]}…{at[-6:]} (expires_in={tok.get('expires_in')}s, "
          f"refresh_token={'yes' if tok.get('refresh_token') else 'no'})")

    print("2) GET /User/Inventory ...")
    inv = api("GET", "/User/Inventory", at, query="?onlyOwnedGateways=false")
    gws = inv.get("gateways") or []
    print(f"   OK — {len(gws)} gateway(s)")
    for gw in gws:
        serial = gw.get("serialNumber")
        ss = (gw.get("socketsState") or {}).get("value") or {}
        dv = ss.get("dimmerValue")
        dv = dv.get("onetValue") if isinstance(dv, dict) else dv
        print(f"   • gateway serial={serial} type={gw.get('gatewayType')} online={gw.get('isOnline')}")
        if ss:
            print(f"       sockets 1={ss.get('socket1')} 2={ss.get('socket2')} 3={ss.get('socket3')} "
                  f"dimmerOn={ss.get('socketDimmer')} dimmerValue={dv}")
        for d in gw.get("devices") or []:
            print(f"       device #{d.get('deviceNumber')} type={d.get('deviceType')} "
                  f"connected={(d.get('connectionState') or {}).get('isConnected')}")

    if not gws:
        return
    serial = gws[0].get("serialNumber")

    if args.set_socket:
        idx = {1: onet.Socket.SOCKET_1, 2: onet.Socket.SOCKET_2, 3: onet.Socket.SOCKET_3}[args.set_socket]
        packet = onet.set_socket_packet(idx, onet.ON if args.on else onet.OFF)
        print(
            f"3) SendONetPacket SET socket {args.set_socket} -> "
            f"{'ON' if args.on else 'OFF'} (serial {serial})"
        )
    else:
        packet = onet.get_scene_packet()
        print(f"3) SendONetPacket GET_LIVE_SCENE (read-only tunnel test, serial {serial})")
    print(f"   sending: {packet.hex()}")
    reply = send_onet(at, serial, packet)
    print(f"   reply  : {reply.hex() or '(empty)'}")
    if reply:
        decoded = onet.parse_packet(reply)
        print(
            f"   -> gateway replied packetType=0x{decoded.packet_type:04x}, "
            f"{len(decoded.payload)} payload bytes"
        )
        if decoded.packet_type == onet.PacketType.GET_LIVE_SCENE:
            try:
                lsr = onet.parse_live_scene_reply(decoded.payload)
                print(f"   -> live scene: {onet.parse_socket_state(lsr.data)}")
            except Exception as e:  # noqa: BLE001
                print(f"   (scene decode: {e})")


if __name__ == "__main__":
    main()
