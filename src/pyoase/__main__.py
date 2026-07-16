"""Command-line interface for pyoase (hardware validation without Home Assistant).

    export OASE_EMAIL=you@example.com OASE_PASSWORD='...'
    python -m pyoase inventory
    python -m pyoase set --gateway <id> --socket 1 --on
    python -m pyoase set --gateway <id> --dimmer 128
    python -m pyoase scene --gateway <id>          # live read via the O-Net tunnel
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import aiohttp

from . import onet
from .auth import OaseAuth
from .client import OaseCloudClient
from .exceptions import OaseError


async def _run(args: argparse.Namespace) -> int:
    email = os.environ.get("OASE_EMAIL")
    password = os.environ.get("OASE_PASSWORD")
    if not email or not password:
        print("set OASE_EMAIL and OASE_PASSWORD", file=sys.stderr)
        return 2

    async with aiohttp.ClientSession() as session:
        auth = OaseAuth(session, email, password)
        client = OaseCloudClient(session, auth)
        try:
            if args.command == "inventory":
                inv = await client.async_get_inventory()
                for gw in inv.gateways:
                    print(f"gateway {gw.id}  serial={gw.serial_number}  type={gw.gateway_type}  "
                          f"online={gw.is_online}")
                    if gw.sockets:
                        s = gw.sockets
                        print(f"  sockets 1={s.socket1} 2={s.socket2} 3={s.socket3} "
                              f"dimmer_on={s.dimmer_on} dimmer={s.dimmer_value}")
                    for d in gw.devices:
                        print(f"  device {d.device_type} #{d.device_number} connected={d.is_connected}")
            elif args.command == "scene":
                print(await client.async_get_scene(args.gateway))
            elif args.command == "set":
                if args.dimmer is not None:
                    ok = await client.async_set_dimmer_value(args.gateway, args.dimmer)
                    print(f"dimmer -> {args.dimmer}: {'ok' if ok else 'failed'}")
                elif args.socket:
                    sockets = {
                        1: onet.Socket.SOCKET_1,
                        2: onet.Socket.SOCKET_2,
                        3: onet.Socket.SOCKET_3,
                    }
                    ok = await client.async_set_socket(args.gateway, sockets[args.socket], args.on)
                    print(f"socket {args.socket} -> {'on' if args.on else 'off'}: {'ok' if ok else 'failed'}")
                else:
                    print("specify --socket N --on/--off or --dimmer 0-255", file=sys.stderr)
                    return 2
        except OaseError as err:
            print(f"error: {err}", file=sys.stderr)
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pyoase")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("inventory", help="list gateways, devices and live state")
    p_scene = sub.add_parser("scene", help="read socket state via the O-Net tunnel")
    p_scene.add_argument("--gateway", required=True)
    p_set = sub.add_parser("set", help="switch a socket or set the dimmer")
    p_set.add_argument("--gateway", required=True)
    p_set.add_argument("--socket", type=int, choices=[1, 2, 3])
    p_set.add_argument("--on", action="store_true")
    p_set.add_argument("--off", action="store_true")
    p_set.add_argument("--dimmer", type=int, metavar="0-255")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
