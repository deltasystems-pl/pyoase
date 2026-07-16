# Getting an OASE token & the OAuth client_id (one-time)

The OASE cloud API uses Azure AD B2C (OAuth2 auth-code + PKCE). There is **no**
username/password grant, so we capture two values once:

| What | Used for |
|------|----------|
| A **Bearer access token** (`Authorization: Bearer eyJ…`) | Prove cloud control against your real device today (`scripts/probe.py`, or Swagger "Try it out"). Expires in ~1 h. |
| The **`client_id`** (a GUID) + `redirect_uri` + `scope` | Lets `pyoase.auth` log in headlessly so the HA config flow only asks for email + password. Permanent. |

## Method A — OASE web portal + browser DevTools (easiest, no proxy, no APK)

1. Open **https://oec.oase-livingwater.com/** and log in with your OASE account.
2. Press **F12** → **Network** tab → enable **Preserve log**. Click around so the app loads your devices.
3. **Get the token:** filter the request list by `oasecloud` (or `azurewebsites`). Click any request to
   `app-oasecloud-prod.azurewebsites.net` → **Headers** → **Request Headers** → copy the whole value
   after `Authorization: Bearer `. Note whether toggling a socket in the portal fires
   `POST …/Gateway/{serial}/SendONetPacket` — that visually confirms the control path.
4. **Get the client_id:** filter by `authorize`. Click the request to
   `account.oase.com/…/oauth2/v2.0/authorize` → **Payload / Query String Parameters** →
   copy `client_id`, `redirect_uri`, and `scope`.

## Method B — phone traffic capture (fallback)

Use **HTTP Toolkit** / **PCAPdroid** / **mitmproxy** on Android, open the **OASE Control** app
(`com.oase.easycontrol`), log in, tap a socket, and read the same `Authorization` header (from
`app-oasecloud-prod…`) and `client_id` (from `account.oase.com/…/authorize`). If TLS is pinned,
decompile the APK (`jadx`) and grep for `client_id` / `oasecustomersprod` / `msauth://`.

## Prove control immediately (recommended)

```bash
export OASE_TOKEN='eyJ...'                    # the Bearer value you copied
python3 scripts/probe.py                       # lists gateways + live socket state (read-only)
python3 scripts/probe.py --gateway <serialNumber> --socket 1 --on
```

If the outlet clicks on, cloud control is confirmed end-to-end.

## Or prove it entirely in Swagger "Try it out"

In the **Authorize** modal: paste **client_id**, leave **client_secret empty**, tick the
`…/api/oase.readwrite` scope, authorize (log in). Then `POST /Gateway/{serialNumber}/SendONetPacket`
with body `{"data": "<base64>"}` using a packet below.

> Caveat: the Authorize button only works if the client_id you paste has the Swagger page registered
> as a redirect URI. The mobile app (redirect `msauth://…`) and the portal (redirect
> `oec.oase-livingwater.com`) may not — if you get `AADB2C90006`, use the DevTools **token** with
> `probe.py` instead; that always works.

| action | base64 for `data` |
|--------|-------------------|
| Socket 1 ON  | `XCNPQQ0AAAACAADEAAAAAAQAAAAAAAAAAGQCAP8=` |
| Socket 1 OFF | `XCNPQQ0AAAACAADEAAAAAAQAAAAAAAAAAGQCAAA=` |
| Socket 2 ON  | `XCNPQQ0AAAACAADEAAAAAAQAAAAAAAAAAGQCAf8=` |
| Socket 2 OFF | `XCNPQQ0AAAACAADEAAAAAAQAAAAAAAAAAGQCAQA=` |
| Socket 3 ON  | `XCNPQQ0AAAACAADEAAAAAAQAAAAAAAAAAGQCAv8=` |
| Socket 3 OFF | `XCNPQQ0AAAACAADEAAAAAAQAAAAAAAAAAGQCAgA=` |
| Dimmer ON    | `XCNPQQ0AAAACAADEAAAAAAQAAAAAAAAAAGQCA/8=` |
| Dimmer 50 % (128) | `XCNPQQ0AAAACAADEAAAAAAQAAAAAAAAAAGQCBIA=` |
| GET all states (safe read) | `XCNPQQUAAAACAADFAAAAAAQAAAAA` |

## Then wire it permanently

Put `client_id` / `redirect_uri` / `scope` into `pyoase/src/pyoase/const.py`
(`B2C_CLIENT_ID`, `B2C_REDIRECT_URI`, `B2C_SCOPES`). After that the token above is never needed
again — `pyoase.auth` logs in with just email + password.
