# pyoase

Async Python client and O-Net protocol codec for **OASE InScenio FM-Master** (Easy Garden
Control / "OASE Control") smart garden & pond power controllers.

It talks to the **OASE cloud** (Azure AD B2C + REST) using just your OASE account email and
password, and controls the device by relaying its native **O-Net** protocol packets through the
cloud's `SendONetPacket` endpoint. This library is the engine behind the
[OASE Home Assistant integration](https://github.com/deltasystems-pl/ha-oase).

> Not affiliated with OASE GmbH. Use at your own risk.

## Install

```bash
pip install pyoase
```

## Quick start

```python
import asyncio, aiohttp
from pyoase import OaseAuth, OaseCloudClient, onet

async def main():
    async with aiohttp.ClientSession() as session:
        auth = OaseAuth(session, "you@example.com", "your-password")
        client = OaseCloudClient(session, auth)

        inv = await client.async_get_inventory()
        gw = next(g for g in inv.gateways if g.is_fm_master)
        print(gw.sockets)  # SocketsState(socket1=..., dimmer_value=...)

        await client.async_set_socket(gw.id, onet.Socket.SOCKET_1, on=True)
        await client.async_set_dimmer_value(gw.id, 128)

        # attached EGC devices (pumps, RGB controllers)
        pump = 1000000001  # device.device_number
        await client.async_set_device_on(gw.id, pump, on=True)   # RDM
        await client.async_set_pump_power(gw.id, pump, 128)      # RDM
        await client.async_set_pump_show(gw.id, 5)               # flow-control "show"

asyncio.run(main())
```

## CLI

```bash
export OASE_EMAIL=you@example.com OASE_PASSWORD='...'
python -m pyoase inventory
python -m pyoase set --gateway <gateway-id> --socket 1 --on
python -m pyoase set --gateway <gateway-id> --dimmer 128
```

## How it works

- **Auth** — the OASE B2C tenant only exposes the interactive `B2C_1A_SignUp_SignIn` policy (no
  password grant), so `OaseAuth` scripts the browser login flow headlessly (authorize → SelfAsserted
  → confirmed → token, PKCE S256) and caches the refresh token. See [docs/REVERSE_ENGINEERING.md](https://github.com/deltasystems-pl/pyoase/blob/main/docs/REVERSE_ENGINEERING.md).
- **Reads** — `GET /User/Inventory` returns fully structured state (`SocketsState`, `PumpState`), no
  packet parsing required.
- **Writes** — `POST /Gateway/{id}/SendONetPacket` relays a raw O-Net packet to the gateway (path
  parameter is the gateway **GUID `id`**, not the serial):
  - **Sockets & dimmer** — `SET_LIVE_SCENE`. `onet.py` is a clean-room port of the MIT-licensed
    ioBroker.oasecontrol codec (see `NOTICE`).
  - **RGB controllers** — `SET_LIVE_SCENE` **scene 5**, a 9-byte per-channel record (colour,
    brightness, effect, speed, on/off); decoded from an app-traffic capture.
  - **Pumps** — **RDM (ANSI E1.20)** tunnelled through O-Net (`rdm.py`): on/off, power, and
    diagnostics (operating hours, firmware). Pump flow-control "shows" use a dedicated packet.
- **Diagnostics** — device model name, firmware version, and operating hours over RDM.

## The `onet` codec (stdlib-only)

```python
from pyoase import onet
pkt = onet.set_socket_packet(onet.Socket.SOCKET_1, onet.ON)   # bytes ready for SendONetPacket
onet.parse_socket_state(scene_bytes)                          # -> SocketState
```

`onet` and the data models import without `aiohttp`, so the codec can be used standalone.

## Development

```bash
python -m pytest        # onet golden vectors + model parsing (no network)
ruff check .
```

## License

MIT. The protocol codec is a clean-room reimplementation; see `NOTICE`.
