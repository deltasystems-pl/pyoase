# OASE FM-Master / OASE Control — reverse-engineering reference

Everything learned about controlling OASE InScenio FM-Master (EGC / "OASE Control") devices.
This is the durable knowledge base; the code in `pyoase` is the executable form of it.

Validated live on **2026-07-15/16** against a real **FM-Master Cloud (ESP)** account.

---

## 1. Devices & families

- **InScenio FM-Master** smart power controller: 3 switchable outlets + 1 dimmable outlet, plus up
  to ~10 attached OASE devices (pumps, filters, LEDs) over the EGC bus.
- Two variants: **Home** (local only) and **Cloud** (local + OASE cloud, remote control). Newer
  units are the **ESP** hardware revision (`...Esp` types). Article numbers: **70788/70789/70790**
  (FM-Master Cloud). The tested unit is `articleNumber 70788`, `gatewayType FmMasterWLanEgcCloudEsp`.
- App: **OASE Control** (Android `com.oase.easycontrol`, built with MSAL.Xamarin.Android). Web
  portal: **oec.oase-livingwater.com**. There is **no** official public API or SDK.

`ControlUnitType` enum (from the cloud OpenAPI): `Invalid, FmMasterWLanGatewayOld,
FmMasterWLanEgcCloud, FmMasterWLanEgcHome, GatewayCloud, GatewayHome, Aquatic, Vivarium,
FmMasterWLanEgcCloudEsp, FmMasterWLanEgcHomeEsp, GatewayCloudEsp, GatewayHomeEsp, AquaticEsp,
BioMaster, ProfiClearGuard, …Staging`.

`DeviceType` enum: `Unknown, AquariumLed, AquariumPump, GardenLed, GardenPump, GardenFilter, Classic`.

---

## 2. Cloud REST API

- Base URL: `https://app-oasecloud-prod.azurewebsites.net`
- Versions: `4.0`, `4.2`, `5.0` — send the requested version in the **`api-version` HTTP header**
  (e.g. `api-version: 5.0`), not the path. 38 endpoints; interactive Swagger at `/index.html`
  (spec at `/swagger/v5.0/swagger.json`). **Authing in Swagger does not reveal new endpoints — the
  schema is static.**
- All endpoints require `Authorization: Bearer <token>` (OAuth2, see §3).

### Key endpoints

| Method & path | Purpose |
|---|---|
| `GET /User/Inventory?onlyOwnedGateways=false` | **The main read.** User + all gateways + devices + live state. |
| `GET /User/Bootstrap` | App bootstrap settings (multi-user, ToU) — not device data. |
| `POST /Gateway/{id}/SendONetPacket?timeout=10` | **The control channel.** Relays a base64 O-Net packet to the gateway, returns the gateway's reply (`ONetPacketData{data}`). |
| `PUT /Gateway/{id}/ActiveDevices` | Set which attached devices are "active" (not socket on/off). |
| `GET /Device/{deviceNumber}/DeviceStateHistory?from&until` | Historical telemetry (returns an array). |
| `GET /Device/{id}/Errors`, `PUT …/Errors/MarkSeen` | Device error list / acknowledge. |
| `PUT /Gateway/{serial}/Register`, `POST …/Unregister` | Pair/unpair a gateway to the account. |

### Inventory shape (the parts that matter)

```
Inventory { user: UserDto, gateways: [Gateway] }
Gateway {
  id: GUID,                        # <-- use THIS for SendONetPacket
  serialNumber: str,               # e.g. "500000000000"
  articleNumber: int,              # 70788
  gatewayType: str,                # "FmMasterWLanEgcCloudEsp"
  isOnline: bool, onlineState: { isOnline, eventTime },
  socketsState: {                  # last-known; cached when offline
    value: { socket1, socket2, socket3: bool, socketDimmer: bool, dimmerValue: 0-255 },
    timestamp
  },
  devices: [Device],
  activeDevices, incrementStates: [IncrementRequestReply], telemetryStates, customAttributesJson
}
Device {
  id, deviceNumber, articleNumber, deviceType,
  connectionState: { isConnected, timestamp },
  dmxPumpState: { value: { fcStatus, fcMode, dimmerValue, deviceOn }, timestamp },   # pumps
  rdmData: { ...raw RDM E1.20... },                                                  # LEDs/pumps
  isActive
}
```

- `dimmerValue` is usually a plain `int`, but the OpenAPI models it as an `OnetByte`
  `{ onetValue, linearValue, percentage, intPercentage }` — parse tolerantly.
- `GardenControlUnitSocket` enum: `Socket1, Socket2, Socket3, DimmerOnOff, DimmerValue`
  (indices 0,1,2,3,4 — matches the O-Net socket indices).
- `EgcFlowControlState` enum (pump `fcStatus`): `SfcOff, DfcOff, SfcOn, DfcOn, ErrorCode`.
- `IncrementRequestReply { requestPacketType, request, replyPacketType, reply }` — the cloud caches
  O-Net command/reply byte-pairs here, confirming control is O-Net-packet based.

### ⚠️ State freshness (measured 2026-07-16)

`socketsState` is **eventually consistent, worst case ~15 s** — it is trustworthy but not instant:
- Reading back **our own** relayed command appears in ~0.5 s (the cloud echoes the command into its
  cache), which is *not* the same as a device push. Do not mistake this for real propagation.
- A change made **elsewhere** (the OASE app, a physical button) reaches the cloud in ~10-15 s.
- The cache can transiently serve a **stale value for ~15 s** after a write (observed: dimmer read
  back `160` for ~15 s after being set to `80`, then corrected). A late device push appears to
  clobber the cache with a pre-command snapshot.
- `socketsState.timestamp` reflects the last **device push**, so it does *not* always advance when
  `value` changes from a relayed command. Do not use it as a freshness check.
- `GET_LIVE_SCENE` (direct O-Net read) is **ground truth** and always agreed with the cache in
  steady state (0/4 disagreements sampled).

Consequence for HA: the coordinator polls at 30 s, so an externally-made change surfaces in ≤30 s
(~15 s mean). Commands issued *from* HA use an optimistic update, because HA additionally debounces
`async_request_refresh` by up to 10 s.

### ⚠️ Gotchas (hard-won)

1. **`SendONetPacket` path param is the gateway GUID `id`, NOT the serial number.** The OpenAPI
   labels it "Serialnumber of gateway" but that is wrong. Serial → **404 Not Found**; GUID id →
   works (or **504 Gateway Timeout** if the device is offline). The 504-vs-404 distinction is how we
   proved this.
2. **Offline devices can't be controlled.** If `isOnline == false` (device unplugged / off Wi-Fi),
   `SendONetPacket` returns **504**. `socketsState` still shows the last-known (cached) values with
   an older `timestamp`. Surface online state and fail commands gracefully.
3. The ESP FM-Master Cloud appears to go offline when idle/unpowered; expect intermittent presence.

---

## 3. Authentication — Azure AD B2C

- Tenant: `oasecustomersprod` (`oasecustomersprod.onmicrosoft.com`, tenant GUID
  `0de5c271-6d54-4b90-93c7-6afb2291325b`). Policy: **`B2C_1A_SignUp_SignIn`**.
- Authorize: `https://account.oase.com/oasecustomersprod.onmicrosoft.com/B2C_1A_SignUp_SignIn/oauth2/v2.0/authorize`
  Token: same base `…/oauth2/v2.0/token`.
- **Public client id** (from the app's authorize URL): `8dfe4495-b83f-4e4f-861c-83b6b3cbaa3b`
  (MSAL.Xamarin.Android). Redirect URI: `msal8dfe4495-b83f-4e4f-861c-83b6b3cbaa3b://auth`.
- Scopes: `https://oasecustomersprod.onmicrosoft.com/api/oase.read
  https://oasecustomersprod.onmicrosoft.com/api/oase.readwrite openid profile offline_access`.
  (`oase.readwrite` is required to control devices.) Flow: **authorization_code + PKCE (S256)**.
- **No ROPC policy** exists (`B2C_1A_ROPC*` / `B2C_1_ROPC` all 404) — you cannot exchange
  username/password for a token directly; the interactive flow must be scripted.

### Headless login flow (proven in `pyoase/auth.py`)

1. `GET …/oauth2/v2.0/authorize` with client_id, redirect_uri, scope, `code_challenge` (S256),
   state, nonce. The returned HTML embeds `var SETTINGS = { transId, csrf, … };` — parse `transId`
   and `csrf`. Cookies `x-ms-cpim-csrf`, `x-ms-cpim-trans`, `x-ms-cpim-cache|…` are set.
2. `POST …/SelfAsserted?tx={transId}&p=B2C_1A_SignUp_SignIn` with header `X-CSRF-TOKEN: {csrf}` and
   form body `request_type=RESPONSE&signInName={email}&password={password}`. Success → `{"status":"200"}`.
3. `GET …/api/CombinedSigninAndSignup/confirmed?rememberMe=false&csrf_token={csrf}&tx={transId}&p=…`
   → **302** to `msal…://auth?code=…&state=…`. Capture `code` from the `Location` header (don't follow
   the custom-scheme redirect).
4. `POST …/oauth2/v2.0/token` with `grant_type=authorization_code`, client_id, scope, code,
   redirect_uri, `code_verifier` → `{ access_token, refresh_token, id_token, expires_in≈3600 }`.
   Refresh with `grant_type=refresh_token`.

### ⚠️ Auth gotchas

1. **Cookie quoting breaks it.** aiohttp's default cookie jar RFC-quotes cookies, which makes the
   `x-ms-cpim-cache|…` cookie invalid → SelfAsserted returns **`400 Bad Request`**. Use
   `aiohttp.CookieJar(quote_cookie=False)`. Because Home Assistant's shared session quotes cookies,
   `OaseAuth` runs the login dance on its **own** private session and only uses the resulting Bearer
   token on the shared session (API calls are cookieless Bearer auth).
2. The `authorize` URL also works with a `/tfp/` segment (the app uses it); both are equivalent.
3. Getting a token without scripting: log into **oec.oase-livingwater.com** in a browser, open
   DevTools → Network, copy the `Authorization: Bearer …` header of any `app-oasecloud-prod` request.
   Handy for quick testing (`scripts/probe.py`). See [CAPTURE.md](CAPTURE.md).

---

## 4. O-Net wire protocol (used by SendONetPacket and the Phase-2 local transport)

Clean-room from the MIT ioBroker.oasecontrol adapter; implemented in `pyoase/onet.py`.

**Frame** = 16-byte little-endian header + payload:

| offset | size | field |
|--------|------|-------|
| 0 | 4 | start delimiter `5C 23 4F 41` |
| 4 | 4 | payload length (uint32 LE) |
| 8 | 1 | version (`0x02`) |
| 9 | 1 | transaction number (0-255, wraps) |
| 10 | 2 | packet type (uint16 LE) |
| 12 | 4 | reserved / zero |
| 16 | N | payload |

### ⭐ The reply-opcode rule (verified 2026-07-16)

**A reply's packet type = the request's packet type with the low byte set to `0xFF`.**
`0xC500`→`0xC5FF`, `0xC400`→`0xC4FF`, `0x1000`→`0x10FF`, `0x4000`→`0x40FF`, `0xB300`→`0xB3FF`, …
Confirmed for every pair the cloud caches in `incrementStates` (§4a) *and* observed live.
Implemented as `onet.reply_type()`.

This retroactively explains the berkinet note "EGC `0x7000/0x70FF`, RDM `0x7100/0x71FF`": those are
**request/reply pairs**, not two opcodes. Both are now **verified live** — `0x7000` = EGC discovery,
`0x7100` = RDM transport (§4c). (RGB *control*, however, turned out to use `SET_LIVE_SCENE` scene 5,
not RDM — see §4c.)

**Transaction number:** the device **ignores** the request's txn and always answers `txn = 0x2b (43)`.
Sending `txn=0` for everything is fine; five back-to-back commands were all accepted.

**Packet types** (requests; all end in `0x00`):

| Type | Name | Notes |
|---|---|---|
| `0x1000` | `DEVICE_INFO` / `DISCOVERY` | 324-byte reply, gateway identity |
| `0x1100` | `ALIVE` | telemetry (`telemetryStates.AliveReply`) |
| `0x1400` | `TCP_REQ` | local transport only |
| `0x3800` | `TIMEZONE` | IANA string, e.g. "Europe/Warsaw" |
| `0x4000` | `DEVICE_TABLE` | payload `[slot u8][5 zero bytes]`; 12 slots (0–11), 76-byte replies |
| `0x8500` | (scheduler) | 1-byte reply |
| `0x9F00` | `PASSWORD_CHECK` | local transport only |
| `0xB300` | `NETWORK_CONFIG` | 87 bytes, contains "OASE FM-Master EGC Cloud" |
| `0xC400` | `SET_LIVE_SCENE` | reply payload = single byte `01` |
| `0xC500` | `GET_LIVE_SCENE` | reply payload = 16 bytes |
| `0xCB00` | `SCHEDULER_STATUS` | |
| `0xD600` | `PROFILES` | |
| `0xD900` | (scheduler) | 0-byte reply |
| `0xDA00` | `GROUPS` | |

**SET_LIVE_SCENE payload (13 bytes)**: `[SceneId=0x04][u32 0][u32 0][SceneType=0x64][SceneLength=0x02][socketIdx][value]`.
- Socket indices: `Socket1=0, Socket2=1, Socket3=2, DimmerOnOff=3, DimmerValue=4`.
- Values: on=`0xFF`, off=`0x00`, dimmer level=`0x00–0xFF` (raw byte; brightness maps 1:1 to 0-255).
- Example — "Socket 1 ON": full packet
  `5c234f41 0d000000 02 00 00c4 00000000 04 00000000 00000000 64 02 00 ff`.

**GET_LIVE_SCENE payload (5 bytes)**: `[SceneId=0x04][u32 0]`. Reply: `parseLiveSceneReply` →
inner 5-byte scene `[s1,s2,s3,s4,dim]` where each socket byte `0xFF`=on; `dim` is 0-255.

**SET reply**: packet type `0xC4FF`, payload = single byte `01`.
⚠️ **`01` = "packet accepted", NOT "an outlet changed"** — a bogus socket index (`0x63`) also
returns `01`. Always read state back to confirm actuation.

**DEVICE_INFO reply (0x10FF, 324-byte payload)** — `onet.parse_discovery_reply`:

| offset | size | field | live value |
|---|---|---|---|
| 0 | 1 | hwType | `8` |
| 1 | 1 | devIdx | `0` |
| 2 | 32 | name (ASCII, NUL-padded) | `FM-Master Cloud` |
| 34 | 12 | serial number | `500000000000` |
| 66 | 64 | long name | `FM-Master EGC Cloud` |
| 134 | 6 | MAC #1 | `24:4c:ab:00:00:01` |
| 140 | 6 | MAC #2 | `24:4c:ab:00:00:00` (may be absent/zero) |
| 151 | 4 | ASCII, unknown | `"240"` — possibly firmware |
| 155 | 5 | ASCII, unknown | `"Oa12"` |

The **string** offsets match ioBroker's `parseDiscoveryReply` exactly. Its **numeric** offsets
(`order`@130, `fw`@187, `rMemVer`@192, `wifiCh`@196, `status`@199) do **not** hold for the Cloud/ESP
variant (they decode to nonsense: `wifiCh=255`, binary `status`), so `pyoase` deliberately omits
them rather than reporting them wrongly. OUI `24:4C:AB` = **Espressif**, and the two MACs differ only
in the last octet — an ESP32 STA/AP pair — corroborating the `...Esp` type. Which is the station
interface is **not** established.

**DEVICE_TABLE reply (0x40FF, 76 bytes/slot)** — `onet.parse_device_table_entry`. Offsets verified:
the article/device numbers cross-check against what the cloud independently reports in
`inventory.gateways[].devices[]`.

| offset | size | field |
|---|---|---|
| 0 | 1 | slot index |
| 4 | 4 | internal handle (u32 LE, sequential per slot) |
| 8 | 4 | **article number** (u32 LE) — `0` ⇒ empty slot |
| 12 | 4 | **device number** (u32 LE) |
| 16 | 2 | magic `"AO"` |
| 20 | 2 | device type code (u16 LE) — `33`=GardenPump, `35`=GardenLed (n=1 each) |
| 24 | … | **product name**, NUL-terminated ASCII |

This is the **only** source of the OASE product name (`Expert 22000`, `RGB Controller`); the
inventory JSON exposes only `deviceType` (`GardenPump`). Empty slots are `0xFF`-filled.

**PASSWORD_CHECK** (local transport only): 64-byte payload = the 74-char cloud device password,
optional `\uXXXX` unescape → UTF-8 → left-aligned into a zero-filled 64-byte buffer.

---

## 4a. ⭐ `incrementStates` — a free, offline-readable O-Net cache

`inventory.gateways[].incrementStates` is a **cloud-side cache of verbatim O-Net request/reply pairs**,
keyed by subsystem. Shape:

```jsonc
{ "key": "Name",
  "value": { "timestamp": "...", "incrementValue": 2,
             "data": [ { "requestPacketType": 4096, "request": "",          // base64
                         "replyPacketType": 4351,  "reply": "CABGTS1N…" } ] } }   // base64
```

The `reply` is the **bare payload — no 16-byte O-Net header** (unlike `SendONetPacket` replies).

Keys seen: `Name` (0x1000), `DeviceTable` (0x4000 ×12 slots), `IanaTimeZone` (0x3800), `Profiles`
(0xD600), `DayPlanEntries`, `NetworkConfiguration` (0xB300), `StaticScenes`, `Groups` (0xDA00),
`SchedulerStatus` (0xCB00/0x8500/0xD900), `DayPlans`.

**Why it matters:** gateway identity and device product names come for free from the ordinary
inventory read — **no relay round-trip, and they work while the gateway is offline**. `pyoase` uses
it for `Gateway.info` and `Device.product_name`.
⚠️ It can be **stale** (the cached `Name` reply was ~2 weeks old and lacked the second MAC). Fine for
identity fields, which don't change; do **not** use it for live state.

Sibling gateway fields worth knowing: `onlineState{isOnline,eventTime}`, `activeDevices{devices[]}`,
`telemetryStates` (`AliveReply` blob, starts with the ASCII serial).

### Phase-2 local transport (no cloud)

The device also speaks this protocol locally, but awkwardly: **UDP 5959** for discovery + a
"connect back" request, then the device dials **out** to a **TLS server the client hosts on TCP 5999**
(reversed connection). TLS uses **legacy ciphers** (`AES128-SHA:DES-CBC3-SHA:RC4-SHA:RC4-MD5:…`,
TLS 1.3 disabled), self-signed cert `CN=com.oase.easycontrol`, no cert validation. Requires the
device's static IP, the host's static IP, the host MAC on the AP broadcast whitelist, and the
sniffed 74-char password (obtainable from the cloud inventory). Deferred; cloud-first avoids all of it.

---

## 4b. ⭐ Attached EGC devices (pumps / LEDs) — how they really work

**The outlet and the device are two independent layers.** Verified live 2026-07-16:

- An outlet (socket 1-3 / dimmer) is just **mains power**.
- An EGC device (e.g. an AquaMax "Expert 22000" pump) plugged into that outlet **also** has its own
  on/off state over the EGC bus — this is `dmxPumpState.value.deviceOn`, and it is what the OASE app
  renders as the device's own switch.

Consequences (user-confirmed):
- Pump enabled (`deviceOn=true`) + socket on ⇒ pump runs.
- Pump **disabled** (`deviceOn=false`) ⇒ toggling the socket appears to **do nothing**. This is
  correct behaviour, not a bug — the pump ignores the power. The integration therefore exposes
  `deviceOn` as a `binary_sensor` ("Switched on") so the cause is visible.
- After the socket is powered back on, the device takes **~20-30 s** to re-attach to the EGC bus
  (`connectionState.isConnected` flips, then `dmxPumpState` repopulates). Its entities are
  `unavailable` meanwhile.

### RDM parameter IDs (`rdmData`, base64 per PID)

`rdmData` is a list of `{key:{parameterId, sensorId?}, value:{value(base64), timestamp}}`. PIDs
≥ `0x8000` are manufacturer-specific (per ANSI E1.20); the rest are standard RDM.

| PID | Std? | Observed | Interpretation |
|---|---|---|---|
| `0x0030` | std | empty (sensor 3) | STATUS_MESSAGES |
| `0x0050` | std | 50 B list of u16 PIDs | **SUPPORTED_PARAMETERS** — enumerates the device's capabilities |
| `0x0060` | std | 19 B | DEVICE_INFO (E1.20) |
| `0x0201` | std | 9 B per sensor | SENSOR_VALUE |
| `0x1010` | std-ish | pump `2`→`255` exactly as `deviceOn` false→true; LED `225` | **tracks the pump's on/off** |
| `0x800D` | mfr | u32 `103` | unknown (runtime/flow?) |
| `0x802F` | mfr | `0` | unknown |
| `0x8038` | mfr | `0` | correlates with `deviceOn=false` |
| `0x8039` | mfr | `93` = `dmxPumpState.dimmerValue` | **pump level** |
| `0x8040` | mfr | `7` (`fcMode` was 5) | unknown |
| `0x8000` | mfr | LED: 36 B | **RGB state — decoded, see below** |
| `0x8002` | mfr | LED: `01 07` → `0f 07` when all 3 channels on | channel enable bitmask? |

### RGB Controller (`GardenLed`) — PID `0x8000` decoded ✅

36 bytes = **4 records × 9 bytes**, one per RGB channel (the app's "RGB 1/2/3"); the 4th is empty.
Confirmed 2026-07-16 by having the user set known values in the app and reading them back:

```
offset:  0   1   2    3           4        5   6      7    8
        [R] [G] [B] [brightness] [effect] [period u16 BE] [?] [on/off]
```

| byte | meaning | evidence |
|---|---|---|
| 0-2 | **R, G, B** | user set "blue" → `00 00 ff`; factory default green → `00 ff 00` |
| 3 | **brightness** 0-255 | user set **85 %** → `0xd9` = 217 = 85.1 % |
| 4 | **effect id** (lookup table, *not* a formula) | see the effect table below |
| 5-6 | **effect period, u16 BE** — exponential in the speed slider | see below |
| 7 | ? | always `00` |
| 8 | **on/off** | `0xff` on / `0x00` off — all three flipped together on "turn all 3 on" |

#### Effect speed = an exponential period in bytes 5-6 (u16 **BE**)

Measured by moving the app's speed slider to known percentages:

| speed slider | bytes 5-6 | u16 BE |
|---|---|---|
| 24 % | `1f 3d` | 7997 |
| 51 % | `02 e4` | 740 |
| 79 % | `00 40` | 64 |

`ln(period)` against speed % has slope **-0.0874** (24→51 %) and **-0.0875** (51→79 %) — the same to
three decimals, so the mapping is exponential, roughly:

```
period ≈ 65535 × (10/65535) ** (speed/100)      # ~65535 at 0 %  →  ~10 at 100 %
speed% ≈ -ln(period / 65535) / 0.0874
```
Predicts 741 for 51 % vs 740 observed. ⚠️ Do **not** read byte 6 alone as a 0-255 speed: `0x3d`=61
happening to be ≈24 % of 255 at one sample was a **coincidence** that this test refuted.

An active effect animates bytes 0-2 (colour mid-cycle), so they are the *live* colour, not a setpoint.

#### Effect ids (byte 4) — ✅ SOLVED, full "Pokaz" (show) menu

Established by selecting every menu entry **in order** while watching byte 4, so each id maps
unambiguously. Ids increase with menu order but are **irregularly spaced — it is a lookup table,
not a formula**:

| idx | effect (Polish locale) | byte 4 |
|---|---|---|
| 0 | brak wyboru (none) | `0x00` (0) |
| 1 | Ogień (fire) | `0x10` (16) |
| 2 | Lód (ice) | `0x1a` (26) |
| 3 | Zorza Polarna (aurora) | `0x2e` (46) |
| 4 | Wieczorny nastrój (evening mood) | `0x33` (51) |
| 5 | Woda (water) | `0x38` (56) |
| 6 | Róża gradient (rose gradient) | `0x3d` (61) |
| 7 | Liliowy gradient (lilac gradient) | `0x42` (66) |
| 8 | Łąka (meadow) | `0x47` (71) |
| 9 | Tęcza (rainbow) | `0x4c` (76) |

`0x2e` = Zorza Polarna was independently observed in an earlier session — an accidental cross-check.

Notes / corrections to earlier guesses:
- **`0x10` is Ogień, not "none".** "Brak wyboru" is `0x00`. Channels sitting at `0x10` (the untouched
  RGB 2/3) are therefore running *Ogień*, not idle.
- **`0x24` (36) is NOT in this menu** — it is an effect reached from a *different* app menu, occupying
  the gap between Lód (26) and Zorza Polarna (46). So byte 4's id space spans more than "Pokaz".
- An earlier `id = 16 + 10 × index` hypothesis fitted two points and was **refuted** by this sweep
  (it predicted Lód→36; the real value is 26).

⚠️ Read-only in v1: this decodes the **state**. The packet that *writes* it is still unknown.

The pump's `SUPPORTED_PARAMETERS` (PID `0x0050`) reads:
`0030 0031 0080 0081 0082 0090 00C1 00C2 0200 0201 0400 0405 1001 1010 800D 800E 802F 8031 8038
8039 8040 8200 8210 8221 800F`.

### ☠️ HAZARD: `SET_LIVE_SCENE` routes on **sceneType**, and IGNORES the SceneId

Learned the hard way on 2026-07-16. Probing pump writes with
`SceneId=0, sceneType=0x64, data=[0x01, 64]` **switched socket 2 off** — the device ignored
`SceneId=0` and executed it as the ordinary socket command "set socket index 1 → value 64".
That cut power to the RGB controller plugged into socket 2, which then dropped off the bus.

Rules for anyone probing writes:
- **`sceneType=0x64` always means "set an outlet"**, whatever SceneId you put in front of it.
  Never use `0x64` while probing another scene.
- The reply is a useless oracle: `01` = "accepted" even for a no-op, and the *probed* scene showing
  no change does **not** mean nothing happened — check the **sockets** and every attached device.
- Scene 0 write attempts: `sceneType=0` → reply `00` (**rejected**); `sceneType=1` → reply `01` but
  the pump did not change. **SceneId 0 is a read-only status view.**
- ⇒ **Pump** control is **not** reachable via `SET_LIVE_SCENE` — it is RDM (`0x7100`, §4c). But note
  the resolution below: **RGB/LED** control *does* use `SET_LIVE_SCENE`, on **scene 5**.

### Live-scene ID scan (read-only, `GET_LIVE_SCENE` with SceneId 0..12)

| SceneId | sceneType | len | data | meaning |
|---|---|---|---|---|
| **0** | 1 | 2 | `19 5d` = `[25, 93]` — the `93` matches the pump level | **read-only pump status view** (not writable) |
| 4 | 101 | 5 | `ff ff ff ff 50` | the FM-Master outlets |
| **5** | 71 | 9 | all zero at rest | **the RGB/LED channel** — writes go here (§4c) |
| 1,2,3,6-12 | 0 | 0 | — | empty |

Resolution: **pump control = RDM** (scene 0 is read-only), **RGB control = `SET_LIVE_SCENE` scene 5**.
Both confirmed live — see §4c.

---

## 4c. ⭐⭐ EGC device control via RDM — SOLVED & shipped (2026-07-16)

Pumps and the RGB controller are **RDM (ANSI E1.20) devices** on the EGC bus, addressed with
standard RDM frames tunnelled through two O-Net packet types. Documented by the `berkinet/oase-fm`
protocol notes (facts only — that repo is unlicensed, no code taken) and **re-verified live**, incl.
a real SET that changed the pump and was confirmed by read-back. Implemented in `pyoase/rdm.py`.

| O-Net type | reply | purpose |
|---|---|---|
| `0x7000` | `0x70FF` | **EGC discovery** — request payload `00`; reply enumerates devices |
| `0x7100` | `0x71FF` | **RDM transport** — payload is a standard RDM frame; reply zero-padded to 257 B |

**Discovery reply**: `u32 discover_only_new`, `u32 count`, then `count` × 12-byte records of
`u32 article`, `u32 deviceNumber`, `u16 manufacturer`, `u16 subdevices` (all LE). Live:
`article=42405 dev=1000000001 mfr=0x4F41 subs=0` (pump) and
`article=42639 dev=1000000002 mfr=0x4F41 subs=3` (RGB — **subs=3 = the 3 channels**).

**UID = manufacturer(0x4F41="OA") ‖ deviceNumber**, big-endian → e.g. pump `4F41:3B9ACA01`. So a
device's RDM UID is derivable from its inventory `deviceNumber`; no discovery call strictly needed.
`0x4F41` is the same "OA"/"AO" magic seen in the device table (§4).

**RDM frame** (`build_frame`): `CC 01 | msgLen | dest(6) | src(6) | txn | portId=1 | msgCount=0 |
subDevice(2 BE) | CC(1) | PID(2 BE) | PDL(1) | data | checksum(2 BE)`. `msgLen = 24 + PDL` and is
also the checksum offset. **Source UID `0000:00000000` is accepted** by the FM-Master (it is the bus
controller). Reply `command_class` = request | `0x01` (GET→0x21, SET→0x31); `response_type 0x00` = ACK.

**Confirmed parameters (live):**

| PID | op | meaning |
|---|---|---|
| `0x1010` | GET/SET | **device on/off** — `0x00` off, `0xFF` on. Verified: SET off→on toggled the pump. |
| `0x8039` | GET/SET | **pump power**, raw 0-255. `raw = floor(pct*255/100)`; app shows `pct = ceil(raw*100/255)` (so 0x7F=50%, 0x80=51%). User's live 23% = raw 57 ✓. |
| `0x0060` | GET | DEVICE_INFO (E1.20) |
| `0x0080` | GET | model description — pump returned **"AQUARIUS 22000"** |
| `0x00C0` | GET | software version label |
| `0x0050` | GET | SUPPORTED_PARAMETERS (which PIDs the device implements) |

### ☠️ CRITICAL: the cloud `dmxPumpState` does NOT reflect RDM writes

`inventory…dmxPumpState` is a cache that only advances on an **app action or a device push** — after
an RDM SET it stayed stale for **40 s+** (deviceOn frozen `true`, timestamp frozen) while the RDM GET
showed the true new state immediately. ⇒ **read pump state via RDM GET, not from the inventory.** The
HA coordinator overlays RDM `0x1010`/`0x8039` reads onto each poll for connected pumps
(`_async_patch_pump`); `fcMode`/`fcStatus` have no RDM equivalent and keep the cached values.

### ☠️ `dmxPumpState` is not a capability flag either (ha-oase#1, 2026-09-16)

`dmxPumpState` carries the **Digital Flow Control** fields, and the cloud publishes the block only
for pumps that *have* DFC — the fountain models. Two reporters with an **AquaMax 13000**
(`F0C4_IM241_AQM_2025-03-13`) on an **EGC / Garden Controller Cloud** get no block at all, while the
same pump answers `0x00C0` and `0x800D` perfectly well, so RDM reaches it end to end.

⇒ **the absence of `dmxPumpState` says nothing about whether a device can be controlled.** Ask the
device: GET `0x1010` and `0x8039` and see what ACKs. Only a *reply* from the device (a NACK, or a
dropped request) writes a parameter off — a transport failure must not, or one bad minute of network
costs a pump its entities until the next reload.

**Probe by reading, never by writing** — see the ☠️ HAZARD notes above for what a blind SET cost us.

Consequences for entity design: on/off and power are RDM and available to any pump that answers;
**shows are not**. There is no RDM equivalent for `fcMode`/`fcStatus`, so a pump without the cloud
block has no way to report show state and must not be offered a show selector.

### ⭐⭐ RGB write — SOLVED & shipped (2026-07-16, from an app capture)

**RGB control does NOT use RDM.** The OASE app writes RGB through **`SET_LIVE_SCENE` (0xC400)** —
the *same* channel as the sockets — with a new **scene id 5**. Captured, decoded, byte-for-byte
reproduced, and verified live from HA. Reads still come from RDM PID `0x8000` (the RDM SET path was a
red herring; ignore the "NACK" section below except as a warning).

**Scene-5 write payload (20 bytes):**
```
05 01 <channel> 00 00 00 00 00 00 | 47 09 | <9-byte channel record>
 │  │     │       └── reserved ──┘   │  └ scene length = 9
 │  │     └ channel 1/2/3            └ scene type = 0x47 (71)
 │  └ constant 0x01
 └ scene id = 0x05 (LED)   (sockets use scene id 0x04, type 0x64, len 2)
```
Reply is the same `0x C4FF` + payload `01` success as a socket set. `onet.build_led_set_payload` /
`onet.set_led_packet`.

**9-byte channel record** (identical to RDM PID `0x8000`'s per-channel record) — `onet.LedRecord`:
```
[R] [G] [B] [brightness 0-255] [effect] [period u16 BE] [reserved=00] [on: FF/00]
```
- **Effect ids** (byte 4), full "Pokaz" menu, confirmed from per-effect captures:
  `NONE 0x00 · Fire 0x10 · Ice 0x1A · Aurora 0x2E · Evening 0x33 · Water 0x38 · Rose 0x3D ·
  Lilac 0x42 · Meadow 0x47 · Rainbow 0x4C`. (Ids from *other* app menus exist too, e.g. `0x0B`,
  `0x24`.)
- **period** (bytes 5-6, u16 BE) = effect speed, larger = slower; app maps a % via an exponential
  curve (samples: 50 %→793, 60 %→335, 76 %→81). Preserve on write via read-modify-write.
- HA maps brightness 0-255 → byte 3 directly, and R/G/B → bytes 0-2.

**Write is a read-modify-write:** to change one field, read the channel's current record (RDM GET
`0x8000` returns all 3), replace the field, send the whole 9-byte record back. HA does this in
`OaseRgbChannelLight` (3 `light` entities per controller, with `rgb_color` + `brightness` + `effect`).

**Pump "shows"** (the Splashy/Wild/Calm flow programs) are a third scene family — **SHIPPED as a
`select`**: packet type **`0x5000`**, payload `00 00 00 00 <enable> <mode>`, reply `0x50FF`.
- `enable=1, mode 1-12` runs a show; `enable=0` (or `mode=0`) turns shows off. Verified live.
- Modes (= `dmxPumpState.fcMode`): 1 Wild · 2 Dynamic · 3 Smooth · 4 Calm · 5 Splashy · 6 Splashy
  small · 7 Calm high · 8 Calm low · 9 Calm wave · 10 Up/down low · 11 Up/down high · 12 Slow.
- **Reading works from the inventory:** unlike pump on/off, `dmxPumpState.fcMode`/`fcStatus` DO track
  shows and update within ~2 s (`fcStatus` = DfcOn while a show runs, DfcOff when off). No RDM needed.

### EGC device diagnostics (RDM) — SHIPPED

Read over RDM from each connected pump/LED:
- **Operating hours** — pump manufacturer PID **`0x800D`** (matches the app's runtime figure, e.g.
  107 h); LEDs use standard **`0x0400`** DEVICE_HOURS. `client.async_get_operating_hours` tries
  `0x800D` then `0x0400`. Exposed as a diagnostic `sensor` (DURATION, hours, total_increasing).
- **Software version** — PID `0x00C0` SOFTWARE_VERSION_LABEL (pump `EC2-MAXI-KOMBI-V05.11-230621`,
  LED `02250605`). Put into the HA device registry's `sw_version` (shown as firmware). Read once and
  cached (it never changes).
- Also available but not exposed: `0x0400` total hours (131), `0x800E` (136), `0x8031` id string,
  `0x0080` model, `0x0081` manufacturer "Oase GmbH", DEVICE_INFO `0x0060`, sensors `0x0201`.

---

### (historical) RGB write — the RDM dead-end (superseded by the capture above)

_Kept as a warning. Before the app capture, the RGB write was wrongly assumed to be an RDM SET of PID
`0x8000`; it is not (the app uses SET_LIVE_SCENE). Details below only matter as a caution._

The RGB controller is an RDM device, model **"LED Driver WG"**, manufacturer "OASE GmbH". Its
`SUPPORTED_PARAMETERS` (`0x0050`) advertises manufacturer PIDs **`0x8000`, `0x8031`, `0x8200`**, plus
standards (`0x0060` DEVICE_INFO, `0x00F0` DMX_START_ADDRESS=9, `0x00E0` DMX_PERSONALITY, `0x0201`
SENSOR_VALUE, `0x0400` DEVICE_HOURS, labels). It also answers `0x8002`, `0x8003`, `0x8221` though they
aren't advertised.

The colour/brightness/effect state is **PID `0x8000`**: a GET on the **root** returns all 36 bytes
(4×9 records; §RGB Controller). GET on sub-devices 1/2/3 NACKs. For a **SET**, the NACK reasons pin
down the addressing (reasons are the real ANSI E1.20 numbers — mind the off-by-one traps):

| SET attempt | NACK reason | meaning |
|---|---|---|
| `0x8000` on root (sub 0), 36 B or 9 B | **9 = SUB_DEVICE_OUT_OF_RANGE** | root is not a valid SET target |
| `0x8000` on sub-device 1/2/3, 9 B | **8 = PACKET_SIZE_UNSUPPORTED** | sub-device is valid; **9 bytes is the wrong size** |

⇒ **RGB write = RDM SET of PID `0x8000` on sub-device 1/2/3 (one per channel), payload size ≠ 9.**
The exact per-channel SET payload (size + field order) is the only missing piece. A size probe is
*mostly* safe because a wrong size is rejected with PACKET_SIZE_UNSUPPORTED before any state change —
but the moment the size is right, whatever bytes you send are applied. **Get the exact bytes from an
app capture instead of guessing** (`CAPTURE.md`: operate one RGB channel, read the `SendONetPacket`
`data`, strip the 16-byte O-Net + 24-byte RDM headers to reveal the parameter payload).

### ☠️ MISTAKE LOG — do not blind-SET manufacturer PIDs to read a NACK reason

While probing, dummy `SET <pid> 00` requests were sent to read NACK reasons. **`0x8002`, `0x8003`,
`0x1010` ACKed and took effect** — `0x8002` (channel-enable byte, `0x0F`=all on) went to `0x00`, which
**switched all three RGB channels off** and zeroed the `0x8000` readback. It was fully recovered by
restoring `0x1010=0xEF`, `0x8002=0x0F 0x07`, `0x8003=0x00 0x23` (the device keeps per-channel colour/
effect in NVM, so re-enabling restored them). Lesson: a SET is only safe to send when you can
read-modify-write the true current value. To learn a PID's writability, read `SUPPORTED_PARAMETERS`
and the command-class support — never fire a dummy SET at live hardware.

Known `0x8000`-neighbour PIDs (LED): `0x8002` = channel-enable (`0x0F`=3 on), `0x8003` = `0x0023`,
`0x8200` = article number `42639` (info), `0x8031` = 16 zero bytes, `0x1010` = dynamic status byte
(`0xE1`/`0xEF`/`0x0F`; **not** a simple on/off like the pump's `0x1010`).

---

## 5. Ecosystem / prior art

- **github.com/mr-suw/ioBroker.oasecontrol** — MIT, JS, the protocol reference (local transport).
- **github.com/berkinet/oase-fm** — Python, **no license** → do NOT copy (legal/HACS blocker); use
  only as reference facts. Adds discovery/RDM notes.
- No existing Home Assistant integration, HACS repo, `home-assistant/brands` entry, FHEM/openHAB
  module, or PyPI package for OASE existed as of 2026-07. Names `oase` (HA domain) and `pyoase`
  (PyPI) were free.
