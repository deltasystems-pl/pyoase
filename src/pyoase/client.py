"""Async client for the OASE cloud REST API."""

from __future__ import annotations

import base64
from typing import Any

import aiohttp

from . import onet, rdm
from .auth import OaseAuth
from .const import API_BASE, API_VERSION, DEFAULT_TIMEOUT, ONET_RELAY_TIMEOUT
from .exceptions import (
    OaseAuthError,
    OaseConnectionError,
    OaseError,
    OaseGatewayOfflineError,
    OaseResponseError,
)
from .models import Inventory, SocketsState


class OaseCloudClient:
    """Talks to the OASE cloud on behalf of one authenticated account.

    Reads go through ``GET /User/Inventory`` (fully structured). Writes go through
    ``POST /Gateway/{id}/SendONetPacket`` — note the path parameter is the gateway
    **GUID id**, not its serial number (the OpenAPI label is misleading).
    """

    def __init__(self, session: aiohttp.ClientSession, auth: OaseAuth) -> None:
        self._session = session
        self._auth = auth

    async def async_get_inventory(self) -> Inventory:
        return Inventory.from_dict(await self.async_get_inventory_raw())

    async def async_get_inventory_raw(self) -> dict[str, Any]:
        """Fetch ``GET /User/Inventory`` as the raw JSON the cloud returned.

        :class:`~pyoase.models.Inventory` deliberately drops what it has no use
        for — notably each device's ``rdmData`` — but those are exactly the
        fields needed to add support for hardware neither of us owns. The Home
        Assistant diagnostics download includes this so a single attachment
        answers "what does this device actually report?".
        """
        return await self._request(
            "GET", "/User/Inventory", params={"onlyOwnedGateways": "false"}
        )

    async def async_send_onet(self, gateway_id: str, packet: bytes) -> bytes:
        """Relay a raw O-Net packet to a gateway; return the gateway's reply bytes."""
        body = {"data": base64.b64encode(packet).decode()}
        data = await self._request(
            "POST",
            f"/Gateway/{gateway_id}/SendONetPacket",
            params={"timeout": str(ONET_RELAY_TIMEOUT)},
            json_body=body,
        )
        reply_b64 = data.get("data") if isinstance(data, dict) else None
        return base64.b64decode(reply_b64) if reply_b64 else b""

    async def async_set_socket(self, gateway_id: str, socket: onet.Socket, on: bool) -> bool:
        """Switch one on/off outlet (Socket1-3 or the dimmer's on/off channel)."""
        packet = onet.set_socket_packet(socket, onet.ON if on else onet.OFF)
        return self._parse_set_reply(await self.async_send_onet(gateway_id, packet))

    async def async_set_dimmer_value(self, gateway_id: str, value: int) -> bool:
        """Set the dimmable outlet level (0-255)."""
        value = max(0, min(255, int(value)))
        packet = onet.set_socket_packet(onet.Socket.DIMMER_VALUE, value)
        return self._parse_set_reply(await self.async_send_onet(gateway_id, packet))

    async def async_get_scene(self, gateway_id: str) -> SocketsState | None:
        """Read live socket state directly from the device via the O-Net tunnel."""
        reply = await self.async_send_onet(gateway_id, onet.get_scene_packet())
        if not reply:
            return None
        decoded = onet.parse_packet(reply)
        scene = onet.parse_live_scene_reply(decoded.payload)
        st = onet.parse_socket_state(scene.data)
        return SocketsState(
            socket1=st.socket1,
            socket2=st.socket2,
            socket3=st.socket3,
            dimmer_on=st.dimmer_on,
            dimmer_value=st.dimmer_value,
        )

    # ---- EGC bus devices (pumps, RGB controllers) via RDM --------------------
    #
    # These are NOT reachable through the socket scenes: an outlet only supplies
    # mains power. An EGC device has its own on/off and level, addressed with RDM
    # frames tunnelled through O-Net. See ``rdm.py``.

    async def async_egc_discover(self, gateway_id: str) -> list[rdm.EgcDevice]:
        """Enumerate the EGC devices attached to a gateway."""
        reply = await self.async_send_onet(
            gateway_id,
            onet.encode_packet(rdm.EGC_DISCOVERY, rdm.discovery_packet_payload()),
        )
        if not reply:
            return []
        return rdm.parse_discovery_reply(onet.parse_packet(reply).payload)

    async def async_rdm_request(
        self,
        gateway_id: str,
        uid: rdm.Uid,
        command_class: int,
        pid: int,
        data: bytes = b"",
        *,
        sub_device: int = 0,
    ) -> rdm.RdmResponse:
        """Send one RDM frame to an EGC device and return its decoded response."""
        frame = rdm.build_frame(uid, command_class, pid, data, sub_device=sub_device)
        reply = await self.async_send_onet(gateway_id, onet.encode_packet(rdm.RDM_REQUEST, frame))
        if not reply:
            raise OaseResponseError(f"empty RDM reply for PID 0x{int(pid):04x}")
        response = rdm.parse_frame(onet.parse_packet(reply).payload)
        if not response.checksum_valid:
            raise OaseResponseError(f"RDM reply checksum mismatch for PID 0x{int(pid):04x}")
        if not response.is_ack:
            raise OaseResponseError(
                f"RDM PID 0x{int(pid):04x} not acknowledged "
                f"(response_type=0x{response.response_type:02x})"
            )
        return response

    async def async_rdm_get(
        self, gateway_id: str, uid: rdm.Uid, pid: int, *, sub_device: int = 0
    ) -> bytes:
        """Read one RDM parameter, returning its raw parameter data."""
        response = await self.async_rdm_request(
            gateway_id, uid, rdm.CommandClass.GET_COMMAND, pid, sub_device=sub_device
        )
        return response.data

    async def async_rdm_set(
        self, gateway_id: str, uid: rdm.Uid, pid: int, data: bytes, *, sub_device: int = 0
    ) -> None:
        """Write one RDM parameter. Raises unless the device acknowledges."""
        await self.async_rdm_request(
            gateway_id, uid, rdm.CommandClass.SET_COMMAND, pid, data, sub_device=sub_device
        )

    async def async_set_device_on(self, gateway_id: str, device_number: int, on: bool) -> None:
        """Switch an attached EGC device (e.g. a pump) on or off.

        This is the device's own state — the same switch the OASE app shows — and is
        independent of the outlet it is plugged into.
        """
        await self.async_rdm_set(
            gateway_id,
            rdm.Uid.for_device(device_number),
            rdm.Pid.DEVICE_ON,
            bytes([onet.ON if on else onet.OFF]),
        )

    async def async_get_device_on(self, gateway_id: str, device_number: int) -> bool:
        """Read an attached EGC device's own on/off state."""
        data = await self.async_rdm_get(
            gateway_id, rdm.Uid.for_device(device_number), rdm.Pid.DEVICE_ON
        )
        return bool(data) and data[0] == onet.ON

    async def async_set_pump_power(self, gateway_id: str, device_number: int, raw: int) -> None:
        """Set a pump's power level (raw 0-255; see ``rdm.percent_to_raw``)."""
        await self.async_rdm_set(
            gateway_id,
            rdm.Uid.for_device(device_number),
            rdm.Pid.PUMP_POWER,
            bytes([max(0, min(255, int(raw)))]),
        )

    async def async_get_pump_power(self, gateway_id: str, device_number: int) -> int:
        """Read a pump's power level as a raw 0-255 byte."""
        data = await self.async_rdm_get(
            gateway_id, rdm.Uid.for_device(device_number), rdm.Pid.PUMP_POWER
        )
        if not data:
            raise OaseResponseError("empty pump power reply")
        return data[0]

    # ---- RGB LED controller (3 channels) -------------------------------------
    #
    # State is read over RDM (PID 0x8000 on the root returns all channels), but
    # writes go through SET_LIVE_SCENE scene 5 — the same channel as the sockets,
    # confirmed by decoding the OASE app's own traffic.

    async def async_get_led_channels(
        self, gateway_id: str, device_number: int
    ) -> list[onet.LedRecord]:
        """Read all RGB channels of an LED controller (RDM PID 0x8000)."""
        data = await self.async_rdm_get(
            gateway_id, rdm.Uid.for_device(device_number), rdm.RGB_STATE_PID
        )
        records: list[onet.LedRecord] = []
        for offset in range(0, len(data) - onet.LED_RECORD_LEN + 1, onet.LED_RECORD_LEN):
            chunk = data[offset : offset + onet.LED_RECORD_LEN]
            # A populated channel has a non-zero record; the 4th slot is all zero.
            if any(chunk):
                records.append(onet.parse_led_record(chunk))
        return records

    async def async_set_led_channel(
        self, gateway_id: str, channel: int, record: onet.LedRecord
    ) -> bool:
        """Write one RGB channel (1-3) via a SET_LIVE_SCENE scene-5 packet."""
        packet = onet.set_led_packet(channel, onet.build_led_record(record))
        return self._parse_set_reply(await self.async_send_onet(gateway_id, packet))

    # ---- pump flow-control "shows" (0x5000) ----------------------------------

    async def async_set_pump_show(self, gateway_id: str, mode: int) -> bool:
        """Set the pump's flow-control show (1-12), or 0 to turn shows off.

        Payload ``00 00 00 00 <enable> <mode>``: enable=1 runs the show, mode 0 /
        enable 0 turns it off (``fcStatus`` → DfcOff). Verified live.
        """
        mode = max(0, min(0xFF, int(mode)))
        enable = 0 if mode == 0 else 1
        packet = onet.encode_packet(onet.PacketType.PUMP_SHOW, bytes([0, 0, 0, 0, enable, mode]))
        return self._parse_set_reply(await self.async_send_onet(gateway_id, packet))

    # ---- EGC device diagnostics (RDM) ----------------------------------------

    async def async_get_supported_parameters(
        self, gateway_id: str, device_number: int
    ) -> tuple[int, ...]:
        """Read the RDM parameters a device declares it supports (PID ``0x0050``).

        Descriptive only. The list is a lower bound (see
        :func:`~pyoase.rdm.parse_supported_parameters`), so it is the right
        thing to put in a diagnostics download and the wrong thing to gate a
        command on — verify a parameter by reading it. Returns an empty tuple
        when the device does not answer.
        """
        try:
            data = await self.async_rdm_get(
                gateway_id,
                rdm.Uid.for_device(device_number),
                rdm.Pid.SUPPORTED_PARAMETERS,
            )
        except OaseError:
            return ()
        return rdm.parse_supported_parameters(data)

    async def async_get_operating_hours(
        self, gateway_id: str, device_number: int
    ) -> int | None:
        """Read a device's operating-hours counter (the app's runtime figure).

        Prefers the manufacturer counter (matches the app); falls back to the
        standard DEVICE_HOURS. Returns None if neither is available.
        """
        uid = rdm.Uid.for_device(device_number)
        for pid in (rdm.Pid.OPERATING_HOURS, rdm.Pid.DEVICE_HOURS):
            try:
                data = await self.async_rdm_get(gateway_id, uid, pid)
            except OaseError:
                continue
            if len(data) >= 4:
                return int.from_bytes(data[:4], "big")
        return None

    async def async_get_software_version(
        self, gateway_id: str, device_number: int
    ) -> str | None:
        """Read a device's software/firmware version label."""
        try:
            data = await self.async_rdm_get(
                gateway_id, rdm.Uid.for_device(device_number), rdm.Pid.SOFTWARE_VERSION_LABEL
            )
        except OaseError:
            return None
        text = data.split(b"\0", 1)[0].decode("ascii", "replace").strip()
        return text or None

    @staticmethod
    def _parse_set_reply(reply: bytes) -> bool:
        if not reply:
            return True  # empty reply on some firmwares still means accepted
        try:
            decoded = onet.parse_packet(reply)
            return onet.parse_set_reply(decoded.payload)
        except ValueError:
            return False

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        _retried: bool = False,
    ) -> Any:
        token = await self._auth.async_get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "api-version": API_VERSION,
            "Accept": "application/json",
        }
        try:
            async with self._session.request(
                method,
                f"{API_BASE}{path}",
                params=params,
                json=json_body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
            ) as resp:
                if resp.status == 401 and not _retried:
                    # token rejected mid-flight: force a fresh login once, then retry
                    await self._auth.async_login()
                    return await self._request(
                        method, path, params=params, json_body=json_body, _retried=True
                    )
                if resp.status in (401, 403):
                    raise OaseAuthError(f"unauthorized ({resp.status})")
                if resp.status == 504:
                    raise OaseGatewayOfflineError("gateway did not respond (likely offline)")
                if resp.status == 404 and path.endswith("SendONetPacket"):
                    raise OaseGatewayOfflineError("gateway not reachable (404)")
                if resp.status >= 400:
                    text = await resp.text()
                    raise OaseResponseError(f"HTTP {resp.status} {method} {path}: {text[:300]}")
                if resp.status == 204 or resp.content_length == 0:
                    return {}
                return await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise OaseConnectionError(f"{method} {path} failed: {err}") from err
