#!/usr/bin/env python3
"""Live proof harness for OASE cloud control — no dependencies beyond the stdlib.

This settles the question "can the cloud API actually control the device?" against
your *real* hardware, without needing the full OAuth flow scripted yet. You supply a
Bearer access token captured once from the OASE Control app (see docs/CAPTURE.md); the
script then:

  1. GET /User/Inventory        -> lists your gateways + live socket state
  2. POST /Gateway/{sn}/SendONetPacket with a SET_LIVE_SCENE packet -> flips a socket
  3. re-reads state to confirm the change round-tripped

Usage:
    export OASE_TOKEN='eyJ...'                 # Bearer token from the capture
    python3 scripts/probe.py                   # read-only: dump inventory + socket state
    python3 scripts/probe.py --gateway 123456789012 --socket 1 --on
    python3 scripts/probe.py --gateway 123456789012 --socket 1 --off
    python3 scripts/probe.py --gateway 123456789012 --dimmer 128     # 0..255 level

Nothing here is destructive beyond toggling the outlet you explicitly name.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

# Make the sibling package importable when run from the repo without installation.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from pyoase import onet  # noqa: E402

API_BASE = "https://app-oasecloud-prod.azurewebsites.net"
API_VERSION = "5.0"


def _request(method: str, path: str, token: str, body: dict | None = None, query: str = "") -> dict:
    url = f"{API_BASE}{path}{query}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("api-version", API_VERSION)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise SystemExit(f"HTTP {e.code} on {method} {path}: {detail}") from e


def get_inventory(token: str) -> dict:
    return _request("GET", "/User/Inventory", token, query="?onlyOwnedGateways=false")


def send_onet(token: str, serial: str, packet: bytes) -> bytes:
    payload = {"data": base64.b64encode(packet).decode()}
    reply = _request("POST", f"/Gateway/{serial}/SendONetPacket", token, body=payload, query="?timeout=10")
    b64 = reply.get("data")
    return base64.b64decode(b64) if b64 else b""


def print_gateways(inv: dict) -> list[dict]:
    gws = inv.get("gateways") or []
    print(f"\nUser: {(inv.get('user') or {}).get('givenName', '?')}  |  {len(gws)} gateway(s)\n")
    for gw in gws:
        ss = (gw.get("socketsState") or {}).get("value") or {}
        print(f"  gateway id={gw.get('id')} serial={gw.get('serialNumber')} "
              f"type={gw.get('gatewayType')} online={gw.get('isOnline')}")
        if ss:
            dv = ss.get("dimmerValue")
            dv = dv.get("onetValue") if isinstance(dv, dict) else dv
            print(f"    sockets: 1={ss.get('socket1')} 2={ss.get('socket2')} "
                  f"3={ss.get('socket3')} dimmerOn={ss.get('socketDimmer')} dimmerValue={dv}")
        for dev in gw.get("devices") or []:
            print(f"    device #{dev.get('deviceNumber')} type={dev.get('deviceType')} "
                  f"connected={(dev.get('connectionState') or {}).get('isConnected')}")
    return gws


def main() -> None:
    ap = argparse.ArgumentParser(description="OASE cloud control proof harness")
    ap.add_argument("--token", default=os.environ.get("OASE_TOKEN"), help="Bearer token (or set OASE_TOKEN)")
    ap.add_argument("--gateway", help="gateway serial number to command")
    ap.add_argument("--socket", type=int, choices=[1, 2, 3], help="socket 1-3 to switch")
    ap.add_argument("--on", action="store_true")
    ap.add_argument("--off", action="store_true")
    ap.add_argument("--dimmer", type=int, metavar="0-255", help="set dimmable outlet level")
    args = ap.parse_args()

    if not args.token:
        raise SystemExit("No token. Capture one from the app (docs/CAPTURE.md) and set OASE_TOKEN.")

    inv = get_inventory(args.token)
    print_gateways(inv)

    if not args.gateway:
        print("\n(read-only) pass --gateway <serial> with --socket/--on/--off or --dimmer to control.")
        return

    if args.dimmer is not None:
        packet = onet.set_socket_packet(onet.Socket.DIMMER_VALUE, max(0, min(255, args.dimmer)))
        label = f"dimmer level -> {args.dimmer}"
    elif args.socket:
        idx = {1: onet.Socket.SOCKET_1, 2: onet.Socket.SOCKET_2, 3: onet.Socket.SOCKET_3}[args.socket]
        value = onet.ON if args.on else onet.OFF
        packet = onet.set_socket_packet(idx, value)
        label = f"socket {args.socket} -> {'ON' if args.on else 'OFF'}"
    else:
        raise SystemExit("Specify --socket N with --on/--off, or --dimmer 0-255.")

    print(f"\nSending SET_LIVE_SCENE ({label}) to gateway {args.gateway} ...")
    print(f"  packet: {packet.hex()}")
    reply = send_onet(args.token, args.gateway, packet)
    print(f"  reply : {reply.hex() or '(empty)'}")
    if reply:
        decoded = onet.parse_packet(reply)
        ok = onet.parse_set_reply(decoded.payload)
        print(f"  gateway acknowledged: {ok}")

    print("\nRe-reading state:")
    print_gateways(get_inventory(args.token))


if __name__ == "__main__":
    main()
