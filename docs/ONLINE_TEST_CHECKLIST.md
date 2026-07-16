# Online verification log — FM-Master EGC Cloud

**All items verified live on 2026-07-16** against a real FM-Master EGC Cloud
(`FmMasterWLanEgcCloudEsp`). This started as a to-do list of things blocked while the test device was
unplugged; it is now the record of what was proven. P0–P2 all pass; P3 (pump/RGB/show control +
diagnostics) is shipped. Remaining open items are noted at the end and are all optional.

Setup to re-verify:
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

## P3 — attached-device control & extras (mostly DONE)

13. **Pump CONTROL.** ✅ **DONE — RDM over O-Net `0x7100`.** Not the scene channel. UID =
    `0x4F41 ‖ deviceNumber`, source `0000:00000000`; PID `0x1010` on/off (`00`/`FF`), `0x8039` power
    (`floor(pct*255/100)`). Shipped as `switch` + `number`. `dmxPumpState` does NOT reflect RDM writes
    → coordinator reads live RDM each poll. See `REVERSE_ENGINEERING.md` §4c. (The earlier "SceneId 0"
    lead was a red herring — scene 0 is a read-only status view.)
13b. **RGB CONTROL.** ✅ **DONE — `SET_LIVE_SCENE` scene 5** (same channel as sockets, NOT RDM), solved
    from an app HTTP capture. 9-byte per-channel record `[R][G][B][bright][effect][period u16BE][0][on]`;
    read via RDM PID `0x8000`. Shipped as `light` ×3 (colour/brightness/10 effects) + `number` speed.
13c. **Pump SHOWS.** ✅ **DONE — O-Net `0x5000`** payload `00×4 <enable> <mode 1-12>`. Shipped as
    `select`. Read from `dmxPumpState.fcMode`/`fcStatus` (these DO update in the inventory).
14. **`rdmData` decoding.** ✅ Mapped; diagnostics shipped (**operating hours** `0x800D`/`0x0400`,
    **firmware** `0x00C0`). RGB `0x8000` fully decoded. See "RDM parameter IDs" in `REVERSE_ENGINEERING.md`.
15. **`DeviceStateHistory`.** ⬜ Still not modelled (optional).
16. **`incrementStates`.** ✅ Used — caches verbatim O-Net request/reply pairs per subsystem, giving
    the gateway identity + device product names for free. `PUT /Gateway/{id}/ActiveDevices` still unused.

**Only genuinely open items:** water-temperature sensor (`SENSOR_VALUE 0x0201`, if present),
`DeviceStateHistory`, and the non-Pokaz RGB "scene/animation" effect ids.

## Endpoints that do NOT exist (404 — don't retry)
`GET /Gateway/{id}` and `GET /Gateway/{id}/Errors` both return `404`. Gateway data comes solely
from `GET /User/Inventory`.

## How to capture app traffic for the remaining P3 items
Use HTTP Toolkit / mitmproxy on the phone (see `CAPTURE.md`), operate the pump/LED in the OASE app,
and record the exact `POST /Gateway/{id}/SendONetPacket` request/response bodies. Decode the base64
`data` with `python -c "import base64,sys; print(base64.b64decode(sys.argv[1]).hex())" <b64>` and
compare against `onet.py`.
