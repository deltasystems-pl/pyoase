# Online verification checklist — FM-Master powered on

Status of the items that were blocked while the test device was unplugged.
**Verified live on 2026-07-16** against an FM-Master EGC Cloud (`FmMasterWLanEgcCloudEsp`).

Setup for testing:
```bash
export OASE_EMAIL=... OASE_PASSWORD=...
cd pyoase && PYTHONPATH=src python -m pyoase inventory     # confirm gateway isOnline=true first
```

---

## P0 — could break the implementation ✅ ALL PASS, no code change required

1. **Does `SendONetPacket`'s returned `data` include the 16-byte O-Net header?**
   ✅ **YES — it is a full framed packet.** `client.async_get_scene()` / `_parse_set_reply()` were
   right to call `onet.parse_packet(reply)` first.
   Request `5c234f41 05000000 02 00 00c5 …` → reply `5c234f41 10000000 02 2b ffc5 …`.

2. **Does `SET_LIVE_SCENE` actually switch the physical outlet?** ✅ **YES.** Socket 1 driven
   OFF then ON; readback confirmed (`ff ff ff ff 54` → `00 ff ff ff 54` → `ff ff ff ff 54`).

3. **What is the real SET reply?** ✅ Payload is a **single byte `01`**, packet type `0xC4FF`.
   `onet.parse_set_reply` (`payload[0] == 1`) is correct.
   ⚠️ **But `01` means "packet accepted", not "an outlet changed"** — a bogus socket index
   (`0x63`) also returns `01`. Never treat it as proof of actuation; read state back instead.

4. **Write scope / authorization.** ✅ `oase.readwrite` accepted; `SendONetPacket` returns `200`,
   never `401/403`.

## P1 — values & UX ✅ verified

5. **Dimmer value semantics.** ✅ **Linear, 1:1, no curve.** Sent 1/64/128/200/255 → read back
   1/64/128/200/255 in both the O-Net scene and `socketsState.dimmerValue`. The `OnetByte`
   `linearValue`/`percentage` fields are **not** used here; the ioBroker `pct*2` mapping is a
   *percentage* API, not the wire format. `light.py`'s 1:1 brightness mapping is correct.

6. **State-propagation latency: inventory vs scene.** ✅ **~0.5 s** — the first poll after a SET
   already reflected it. No optimistic update needed; `async_request_refresh()` after a command
   is sufficient. Note `socketsState.timestamp` does *not* always advance when `value` changes.

7. **`GET_LIVE_SCENE` round-trip.** ✅ Matches both the physical device and the inventory.

8. **Transaction number.** ✅ Non-issue. The device **ignores** the request txn and always replies
   `txn = 0x2b (43)`. Five back-to-back commands with `txn=0` were all accepted; nothing is
   deduplicated. No counter needed.

9. **`SendONetPacket` timeout / latency.** ✅ **0.18–0.41 s** per round-trip (avg 0.19 s over 5).
   The `?timeout=10` relay parameter is generous; the 30 s HTTP timeout is ample.

## P2 — behaviour & robustness

10. **Concurrent app session.** ✅ **No exclusivity.** The integration and the OASE app were used
    simultaneously with no conflict; changes made in the app appear in the cloud (and so in HA)
    within ~10-15 s, and vice-versa. No single-session locking.
11. **Online/offline transitions.** ✅ `isOnline` was `false` while unplugged, `true` once powered on;
    `gateway.onlineState.eventTime` carries the transition time. Attached EGC devices take
    **~20-30 s** to re-attach after their outlet is powered back on (their entities are
    `unavailable` meanwhile).
12. **Malformed/edge commands.** ✅ A bad delimiter returns **`422`** with
    `"Unable to parse packet as ONet packet."` (`OaseResponseError`). ⚠️ A *well-framed* packet with
    a bogus socket index returns **`200` + `01`** — the device does not validate the index (see P0-3).

## P3 — future reverse-engineering (enables more features)

13. **Attached-device (pump / LED) CONTROL.** ⬜ **The main remaining feature gap** — the user wants
    the pump switchable from HA (the app shows "Expert 22000" as a switch). Much better understood now:
    - **Socket ≠ device.** The outlet is only mains power; the pump also has its own EGC on/off
      (`dmxPumpState.deviceOn`). Disabled pump ⇒ the socket switch appears to do nothing. v1 now
      exposes `deviceOn` as a read-only `binary_sensor` so this is at least visible.
    - The **reply opcode rule is proven**: reply = request with the low byte set to `0xFF`
      (`0xC500`→`0xC5FF`, `0x1000`→`0x10FF`, `0x4000`→`0x40FF`, …). The berkinet note about
      "EGC `0x7000/0x70FF`, RDM `0x7100/0x71FF`" therefore describes **request/reply pairs**, not
      two separate opcodes ⇒ the pump/LED *request* opcodes are plausibly `0x7000` / `0x7100`.
    - **RDM PID `0x1010` tracks pump on/off** (`2`→`255` exactly as `deviceOn` false→true).
      `0x8039` = pump level (93). See the RDM table in `REVERSE_ENGINEERING.md`.
    - **Live-scene IDs 0 and 5 exist** besides the outlets' 4 — SceneId 0 is `sceneType=1, len=2,
      data=[25, 93]` and that `93` matches the pump level. A promising but **unconfirmed** lead.
    - **Write path still unproven. Do NOT guess-write to a pump** — capture the app first.
    - Best next step: HTTP Toolkit on the phone, toggle "Expert 22000" in the app, and diff the
      `SendONetPacket` body against SceneId 0 / PID `0x1010` / opcode `0x7000`.
14. **`rdmData` decoding.** 🟡 Substantially mapped, see "RDM parameter IDs" in
    `REVERSE_ENGINEERING.md`. Pump PID `0x0050` (SUPPORTED_PARAMETERS) enumerates its capabilities.
15. **`DeviceStateHistory`.** ⬜ Not modelled.
16. **`ActiveDevices` / `incrementStates`.** ✅ **`incrementStates` turned out to be a goldmine** and
    is now used: it caches verbatim O-Net request/reply pairs per subsystem, giving both the gateway
    identity and the device table for free. See `REVERSE_ENGINEERING.md`.
    `PUT /Gateway/{id}/ActiveDevices` still unused.

## Endpoints that do NOT exist (404 — don't retry)
`GET /Gateway/{id}` and `GET /Gateway/{id}/Errors` both return `404`. Gateway data comes solely
from `GET /User/Inventory`.

## How to capture app traffic for the remaining P3 items
Use HTTP Toolkit / mitmproxy on the phone (see `CAPTURE.md`), operate the pump/LED in the OASE app,
and record the exact `POST /Gateway/{id}/SendONetPacket` request/response bodies. Decode the base64
`data` with `python -c "import base64,sys; print(base64.b64decode(sys.argv[1]).hex())" <b64>` and
compare against `onet.py`.
