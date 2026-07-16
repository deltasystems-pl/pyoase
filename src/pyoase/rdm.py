"""RDM (ANSI E1.20) over the OASE O-Net transport.

OASE's EGC bus devices — pumps, LED/RGB controllers — are RDM devices. They are not
reachable through the FM-Master's socket scenes (those only address the four mains
outlets); they are addressed with standard RDM frames tunnelled inside two O-Net
packet types::

    EGC_DISCOVERY  0x7000  ->  reply 0x70FF   enumerate attached devices
    RDM_REQUEST    0x7100  ->  reply 0x71FF   carry a standard RDM frame

This module is stdlib-only and contains no transport: :class:`~pyoase.client.OaseCloudClient`
relays the bytes built here through the cloud ``SendONetPacket`` endpoint.

The RDM frame layout is the public ANSI E1.20 standard. The OASE-specific parts —
the two O-Net packet types, the discovery record layout, the manufacturer id
``0x4F41`` ("OA"), and the two AquaMax parameter ids — are facts documented by the
``berkinet/oase-fm`` protocol notes and independently re-verified live against an
FM-Master EGC Cloud (see ``docs/REVERSE_ENGINEERING.md``). No code was copied from
that project, which is unlicensed.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

__all__ = [
    "EGC_DISCOVERY",
    "RDM_REQUEST",
    "OASE_MANUFACTURER_ID",
    "RGB_STATE_PID",
    "CommandClass",
    "Pid",
    "ResponseType",
    "Uid",
    "EgcDevice",
    "RdmResponse",
    "build_frame",
    "parse_frame",
    "discovery_packet_payload",
    "parse_discovery_reply",
    "percent_to_raw",
    "raw_to_percent",
]

#: O-Net packet type carrying an EGC discovery request (reply: ``0x70FF``).
EGC_DISCOVERY = 0x7000
#: O-Net packet type carrying an RDM frame (reply: ``0x71FF``).
RDM_REQUEST = 0x7100

#: OASE's RDM manufacturer id — ASCII "OA". Also the ``"AO"`` magic in the device table.
OASE_MANUFACTURER_ID = 0x4F41

_SC_RDM = 0xCC
_SC_SUB_MESSAGE = 0x01
#: Bytes preceding the parameter data in an RDM frame; also the checksum's offset.
_RDM_OVERHEAD = 24
_DISCOVERY_RECORD_LEN = 12


class CommandClass(IntEnum):
    """RDM command classes (E1.20)."""

    GET_COMMAND = 0x20
    GET_COMMAND_RESPONSE = 0x21
    SET_COMMAND = 0x30
    SET_COMMAND_RESPONSE = 0x31


class ResponseType(IntEnum):
    """RDM response types (E1.20)."""

    ACK = 0x00
    ACK_TIMER = 0x01
    NACK_REASON = 0x02
    ACK_OVERFLOW = 0x03


class Pid(IntEnum):
    """RDM parameter ids used by OASE EGC devices.

    ``0x0000``-``0x7FFF`` are standard E1.20; ``0x8000``+ are manufacturer-specific.
    """

    SUPPORTED_PARAMETERS = 0x0050
    DEVICE_INFO = 0x0060
    DEVICE_MODEL_DESCRIPTION = 0x0080
    MANUFACTURER_LABEL = 0x0081
    DEVICE_LABEL = 0x0082
    SOFTWARE_VERSION_LABEL = 0x00C0
    SENSOR_VALUE = 0x0201
    DEVICE_HOURS = 0x0400
    #: Manufacturer operating-hours counter; matches the OASE app's runtime figure.
    OPERATING_HOURS = 0x800D
    #: Device on/off. ``0x00`` = off, ``0xFF`` = on. Verified live on an AquaMax pump.
    DEVICE_ON = 0x1010
    #: Pump power level, one raw byte 0-255. Verified live on an AquaMax pump.
    PUMP_POWER = 0x8039
    #: RGB LED state: 4×9-byte channel records (read). Writes use SET_LIVE_SCENE.
    RGB_STATE = 0x8000


#: Convenience alias for reading the RGB controller's channel records.
RGB_STATE_PID = Pid.RGB_STATE


@dataclass(frozen=True)
class Uid:
    """A 6-byte RDM unique id: 16-bit manufacturer + 32-bit device id."""

    manufacturer: int
    device: int

    def __bytes__(self) -> bytes:
        return struct.pack(">HI", self.manufacturer, self.device)

    def __str__(self) -> str:
        return f"{self.manufacturer:04X}:{self.device:08X}"

    @classmethod
    def from_bytes(cls, raw: bytes) -> Uid:
        if len(raw) != 6:
            raise ValueError("a UID is exactly 6 bytes")
        manufacturer, device = struct.unpack(">HI", raw)
        return cls(manufacturer, device)

    @classmethod
    def for_device(cls, device_number: int) -> Uid:
        """UID of an OASE device from its inventory ``deviceNumber``."""
        return cls(OASE_MANUFACTURER_ID, int(device_number))


#: Source UID used for requests. The FM-Master is the bus controller and accepts a
#: null source; verified live.
CONTROLLER_UID = Uid(0x0000, 0x00000000)


@dataclass(frozen=True)
class EgcDevice:
    """One device from an EGC discovery reply."""

    article_number: int
    device_number: int
    manufacturer_id: int
    subdevice_count: int

    @property
    def uid(self) -> Uid:
        return Uid(self.manufacturer_id, self.device_number)


@dataclass(frozen=True)
class RdmResponse:
    """A decoded RDM response frame."""

    destination: Uid
    source: Uid
    response_type: int
    command_class: int
    pid: int
    data: bytes
    checksum_valid: bool

    @property
    def is_ack(self) -> bool:
        return self.response_type == ResponseType.ACK


def build_frame(
    destination: Uid,
    command_class: int,
    pid: int,
    data: bytes = b"",
    *,
    source: Uid = CONTROLLER_UID,
    sub_device: int = 0,
    transaction_number: int = 0,
    port_id: int = 1,
) -> bytes:
    """Build a standard E1.20 RDM frame, including the trailing 16-bit checksum.

    ``sub_device`` addresses a channel of a multi-channel device (an RGB controller
    reports ``subdevice_count=3``); ``0`` is the root device.
    """
    if len(data) > 231:
        raise ValueError("RDM parameter data must be <= 231 bytes")
    if not 0 <= sub_device <= 0xFFFF:
        raise ValueError("sub_device out of range")
    frame = bytearray((_SC_RDM, _SC_SUB_MESSAGE, _RDM_OVERHEAD + len(data)))
    frame += bytes(destination)
    frame += bytes(source)
    frame.append(transaction_number & 0xFF)
    frame.append(port_id)
    frame.append(0)  # message count: always 0 in a request
    frame += struct.pack(">H", sub_device)
    frame.append(int(command_class))
    frame += struct.pack(">H", int(pid))
    frame.append(len(data))
    frame += data
    frame += struct.pack(">H", sum(frame) & 0xFFFF)
    return bytes(frame)


def parse_frame(payload: bytes) -> RdmResponse:
    """Decode an RDM frame from a ``0x71FF`` reply payload.

    The reply is zero-padded (to 257 bytes); the real length comes from the frame's
    own message-length field.
    """
    if len(payload) < _RDM_OVERHEAD + 2:
        raise ValueError("payload too short for an RDM frame")
    if payload[0] != _SC_RDM or payload[1] != _SC_SUB_MESSAGE:
        raise ValueError("not an RDM frame (bad start code)")
    message_length = payload[2]
    if message_length < _RDM_OVERHEAD or len(payload) < message_length + 2:
        raise ValueError("RDM message length is out of range")
    pdl = payload[23]
    if _RDM_OVERHEAD + pdl > message_length:
        raise ValueError("RDM parameter data length overruns the message")
    expected = struct.unpack_from(">H", payload, message_length)[0]
    return RdmResponse(
        destination=Uid.from_bytes(payload[3:9]),
        source=Uid.from_bytes(payload[9:15]),
        response_type=payload[16],
        command_class=payload[20],
        pid=struct.unpack_from(">H", payload, 21)[0],
        data=bytes(payload[_RDM_OVERHEAD : _RDM_OVERHEAD + pdl]),
        checksum_valid=expected == (sum(payload[:message_length]) & 0xFFFF),
    )


def discovery_packet_payload(discover_only_new: bool = False) -> bytes:
    """Payload for an ``EGC_DISCOVERY`` request (a single flag byte)."""
    return bytes([1 if discover_only_new else 0])


def parse_discovery_reply(payload: bytes) -> list[EgcDevice]:
    """Decode a ``0x70FF`` discovery reply into its device records.

    Layout: ``u32 discover_only_new``, ``u32 count``, then ``count`` 12-byte records
    of ``u32 article``, ``u32 device id``, ``u16 manufacturer``, ``u16 subdevices``
    (all little-endian).
    """
    if len(payload) < 8:
        raise ValueError("discovery reply too short")
    count = struct.unpack_from("<I", payload, 4)[0]
    devices: list[EgcDevice] = []
    for index in range(count):
        offset = 8 + index * _DISCOVERY_RECORD_LEN
        if offset + _DISCOVERY_RECORD_LEN > len(payload):
            break  # truncated reply: return what is intact
        article, device, manufacturer, subdevices = struct.unpack_from("<IIHH", payload, offset)
        devices.append(EgcDevice(article, device, manufacturer, subdevices))
    return devices


def percent_to_raw(percent: float) -> int:
    """Convert a 0-100 percentage to the raw 0-255 byte, as the OASE app does."""
    percent = max(0.0, min(100.0, float(percent)))
    return int(percent * 255 // 100)


def raw_to_percent(raw: int) -> int:
    """Convert a raw 0-255 byte to the percentage the OASE app displays.

    The app rounds **up**, so raw 0x7F shows as 50% and 0x80 as 51%.
    """
    raw = max(0, min(255, int(raw)))
    return -(-raw * 100 // 255)
