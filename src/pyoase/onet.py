"""O-Net protocol codec for OASE InScenio FM-Master (EGC / "OASE Control") devices.

This is a clean-room Python port of the packet layer from the MIT-licensed
ioBroker.oasecontrol adapter (https://github.com/mr-suw/ioBroker.oasecontrol),
specifically ``lib/oase/protocol.js`` and ``lib/oase/index.js``. It contains **no**
transport code: the same byte strings are used both for a direct local TLS socket
(Phase 2) and — base64-encoded — as the body of the cloud
``POST /Gateway/{id}/SendONetPacket`` relay endpoint (v1).

The wire format is a 16-byte little-endian header followed by a variable payload::

    offset  size  field
    0       4     start delimiter  5C 23 4F 41
    4       4     payload length   (uint32 LE)
    8       1     version          (== 2)
    9       1     transaction no.  (0..255, wraps)
    10      2     packet type      (uint16 LE)
    12      4     reserved / zero
    16      N     payload
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from enum import IntEnum

__all__ = [
    "START_DELIMITER",
    "PROTOCOL_VERSION",
    "PacketType",
    "Socket",
    "ON",
    "OFF",
    "OnetPacket",
    "SocketState",
    "LiveSceneReply",
    "DiscoveryInfo",
    "DeviceTableEntry",
    "DEVICE_TABLE_SLOTS",
    "LedEffect",
    "LED_EFFECT_NAMES",
    "PUMP_SHOW_NAMES",
    "LED_RECORD_LEN",
    "LedRecord",
    "parse_led_record",
    "build_led_record",
    "build_led_set_payload",
    "set_led_packet",
    "led_speed_to_period",
    "led_period_to_speed",
    "reply_type",
    "encode_packet",
    "parse_packet",
    "build_socket_set_payload",
    "build_socket_get_payload",
    "set_socket_packet",
    "get_scene_packet",
    "device_table_packet",
    "parse_live_scene_reply",
    "parse_socket_state",
    "parse_set_reply",
    "parse_discovery_reply",
    "parse_device_table_entry",
    "encode_password_payload",
    "password_check_packet",
]

START_DELIMITER = b"\x5c\x23\x4f\x41"
PROTOCOL_VERSION = 2
_HEADER_LEN = 16


class PacketType(IntEnum):
    """O-Net *request* packet type codes (uint16 LE on the wire).

    ``DEVICE_INFO`` and ``DISCOVERY`` share the value 4096 in the reference
    implementation; ``DISCOVERY`` is exposed as an explicit alias.

    Requests always end in ``0x00``; the device answers with the same high byte
    and a low byte of ``0xFF`` (see :func:`reply_type`). The types below the
    fold were recovered from the cloud's own ``incrementStates`` cache, which
    stores request/reply pairs verbatim.
    """

    DEVICE_INFO = 0x1000  # 4096
    DISCOVERY = 0x1000  # 4096 (alias of DEVICE_INFO)
    ALIVE = 0x1100  # 4352
    TCP_REQ = 0x1400  # 5120
    PASSWORD_CHECK = 0x9F00  # 40704
    SET_LIVE_SCENE = 0xC400  # 50176
    GET_LIVE_SCENE = 0xC500  # 50432

    # Recovered from incrementStates (request/reply pairs observed live).
    DEVICE_TABLE = 0x4000  # 16384 — payload: slot index u8 + 5 zero bytes
    TIMEZONE = 0x3800  # 14336 — IANA timezone string
    NETWORK_CONFIG = 0xB300  # 45824
    SCHEDULER_STATUS = 0xCB00  # 51968
    PROFILES = 0xD600  # 54784
    GROUPS = 0xDA00  # 55808
    PUMP_SHOW = 0x5000  # 20480 — pump flow-control "show"; payload 00×4 enable mode


#: Pump flow-control "show" programs (the ``fcMode`` value), captured from the app.
PUMP_SHOW_NAMES: dict[int, str] = {
    0: "Off",
    1: "Wild",
    2: "Dynamic",
    3: "Smooth",
    4: "Calm",
    5: "Splashy",
    6: "Splashy small",
    7: "Calm high",
    8: "Calm low",
    9: "Calm wave",
    10: "Up and down low",
    11: "Up and down high",
    12: "Slow",
}


def reply_type(request_type: int) -> int:
    """Return the reply packet type the device answers a request with.

    The device echoes the request's high byte and sets the low byte to ``0xFF``
    (``0xC500`` -> ``0xC5FF``). Verified live for every request type the cloud
    caches in ``incrementStates`` (0x1000/0x4000/0x3800/0xB300/0xD600/0xDA00/
    0xCB00/0x8500/0xD900) as well as for the live-scene get/set pair.
    """
    return (int(request_type) & 0xFF00) | 0xFF


class Socket(IntEnum):
    """Addressable socket channels of an FM-Master.

    Matches both the ioBroker item ids (``main.js``) and the cloud API's
    ``GardenControlUnitSocket`` enum. Sockets 1-3 are plain on/off outlets;
    socket 4 is the dimmable outlet, split into an on/off channel and a
    separate 0-255 level channel.
    """

    SOCKET_1 = 0x00
    SOCKET_2 = 0x01
    SOCKET_3 = 0x02
    DIMMER_ONOFF = 0x03
    DIMMER_VALUE = 0x04


#: Payload byte meaning "on" for an on/off channel.
ON = 0xFF
#: Payload byte meaning "off" for an on/off channel.
OFF = 0x00

# Live-scene framing constants (see createFmMasterSocketSceneSet / ...Get).
_SCENE_ID = 4
_SCENE_TYPE = 100  # 0x64
_SCENE_LENGTH = 2

# LED (RGB controller) live-scene, captured from the OASE app. The write goes
# through SET_LIVE_SCENE just like the sockets, but with scene id 5 / type 0x47
# and a 9-byte per-channel record. Channels are 1..3.
_LED_SCENE_ID = 5
_LED_SUB_ID = 1  # constant byte after the scene id in every captured LED write
_LED_SCENE_TYPE = 0x47  # 71
#: Length of one RGB channel record, in bytes.
LED_RECORD_LEN = 9
_LED_RECORD_LEN = LED_RECORD_LEN


class LedEffect(IntEnum):
    """RGB effect ids (byte 4 of a LED record); the app's "Pokaz" menu.

    Verified from captured writes; the ids are an irregular lookup table.
    """

    NONE = 0x00  # "brak wyboru" — solid colour
    FIRE = 0x10  # Ogień
    ICE = 0x1A  # Lód
    AURORA = 0x2E  # Zorza Polarna
    EVENING = 0x33  # Wieczorny nastrój
    WATER = 0x38  # Woda
    ROSE = 0x3D  # Róża gradient
    LILAC = 0x42  # Liliowy gradient
    MEADOW = 0x47  # Łąka
    RAINBOW = 0x4C  # Tęcza


#: Human-facing effect names, in menu order, keyed by id.
LED_EFFECT_NAMES: dict[int, str] = {
    LedEffect.NONE: "None",
    LedEffect.FIRE: "Fire",
    LedEffect.ICE: "Ice",
    LedEffect.AURORA: "Aurora",
    LedEffect.EVENING: "Evening",
    LedEffect.WATER: "Water",
    LedEffect.ROSE: "Rose",
    LedEffect.LILAC: "Lilac",
    LedEffect.MEADOW: "Meadow",
    LedEffect.RAINBOW: "Rainbow",
}

# DEVICE_INFO / DISCOVERY reply framing (payload is exactly 324 bytes).
_DISCOVERY_LEN = 324
_DISCOVERY_MAC1 = 134
_DISCOVERY_MAC2 = 140

# DEVICE_TABLE reply framing (payload is 76 bytes per slot; 12 slots, 0..11).
_DEVICE_TABLE_NAME = 24
#: Number of device-table slots an FM-Master exposes.
DEVICE_TABLE_SLOTS = 12


@dataclass(frozen=True)
class OnetPacket:
    """A decoded O-Net frame."""

    packet_type: int
    payload: bytes
    transaction_number: int = 0
    version: int = PROTOCOL_VERSION


@dataclass(frozen=True)
class SocketState:
    """Decoded FM-Master socket scene (the reply to ``GET_LIVE_SCENE``)."""

    socket1: bool
    socket2: bool
    socket3: bool
    dimmer_on: bool
    dimmer_value: int  # 0-255


@dataclass(frozen=True)
class LiveSceneReply:
    """Structured ``GET_LIVE_SCENE`` reply wrapper (mirrors parseLiveSceneReply)."""

    type: int
    id: int
    count: int
    scene_type: int
    scene_length: int
    data: bytes


@dataclass(frozen=True)
class DiscoveryInfo:
    """Identity of a gateway, decoded from a ``DEVICE_INFO``/``DISCOVERY`` reply.

    Only fields verified against a real FM-Master EGC Cloud are exposed. The
    reference implementation also decodes ``order``/``fw``/``wifiCh``/``status``
    from this payload, but those offsets do **not** hold for the Cloud (ESP)
    variant, so they are deliberately omitted rather than reported wrongly.
    """

    hw_type: int
    device_index: int
    name: str  # e.g. "FM-Master Cloud"
    serial_number: str  # e.g. "500000000000"
    long_name: str  # e.g. "FM-Master EGC Cloud"
    mac_addresses: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeviceTableEntry:
    """One populated slot of the gateway's device table (``DEVICE_TABLE`` reply).

    ``name`` is the OASE product name as shown in the app (e.g. "Expert 22000",
    "RGB Controller"), which the cloud inventory does not otherwise expose.
    """

    slot: int
    article_number: int
    device_number: int
    name: str
    type_code: int


def encode_packet(packet_type: int, payload: bytes = b"", transaction_number: int = 0) -> bytes:
    """Build a full O-Net frame (16-byte header + payload).

    Mirrors ``OaseProtocol.createPacket``.
    """
    if not 0 <= transaction_number <= 0xFF:
        raise ValueError("transaction_number must be 0..255")
    header = bytearray(_HEADER_LEN)
    header[0:4] = START_DELIMITER
    struct.pack_into("<I", header, 4, len(payload))
    header[8] = PROTOCOL_VERSION
    header[9] = transaction_number & 0xFF
    struct.pack_into("<H", header, 10, int(packet_type))
    return bytes(header) + payload


def parse_packet(data: bytes) -> OnetPacket:
    """Decode a full O-Net frame. Mirrors ``OaseProtocol.parsePacket``."""
    if len(data) < _HEADER_LEN:
        raise ValueError("invalid packet size")
    if data[0:4] != START_DELIMITER:
        raise ValueError("invalid start delimiter")
    (length,) = struct.unpack_from("<I", data, 4)
    version = data[8]
    txn = data[9]
    (packet_type,) = struct.unpack_from("<H", data, 10)
    payload = data[_HEADER_LEN:]
    if len(payload) < length:
        raise ValueError("truncated payload")
    return OnetPacket(
        packet_type=packet_type,
        payload=payload[:length],
        transaction_number=txn,
        version=version,
    )


def build_socket_set_payload(socket_idx: int, value: int) -> bytes:
    """Build the 13-byte SET_LIVE_SCENE payload. Mirrors createFmMasterSocketSceneSet."""
    if not 0 <= int(socket_idx) <= 0xFF:
        raise ValueError("socket_idx out of range")
    if not 0 <= int(value) <= 0xFF:
        raise ValueError("value must be 0..255")
    buf = bytearray(13)
    buf[0] = _SCENE_ID
    # bytes 1..8 stay zero (two zero uint32 fields)
    buf[9] = _SCENE_TYPE
    buf[10] = _SCENE_LENGTH
    buf[11] = int(socket_idx)
    buf[12] = int(value)
    return bytes(buf)


def build_socket_get_payload() -> bytes:
    """Build the 5-byte GET_LIVE_SCENE payload. Mirrors createFmMasterSocketSceneGet."""
    buf = bytearray(5)
    buf[0] = _SCENE_ID
    return bytes(buf)


def set_socket_packet(socket_idx: int, value: int, transaction_number: int = 0) -> bytes:
    """Full SET_LIVE_SCENE frame to drive one socket channel."""
    return encode_packet(
        PacketType.SET_LIVE_SCENE,
        build_socket_set_payload(socket_idx, value),
        transaction_number,
    )


def get_scene_packet(transaction_number: int = 0) -> bytes:
    """Full GET_LIVE_SCENE frame to read all socket states."""
    return encode_packet(PacketType.GET_LIVE_SCENE, build_socket_get_payload(), transaction_number)


@dataclass(frozen=True)
class LedRecord:
    """One RGB channel's state (the 9-byte LED scene record).

    ``period`` is the effect speed as a raw 16-bit value (larger = slower); the
    app maps it to a percentage on an exponential curve. ``reserved`` (byte 7) is
    always 0 in captures and is preserved verbatim on write.
    """

    red: int
    green: int
    blue: int
    brightness: int  # 0-255
    effect: int  # a LedEffect id
    period: int  # effect speed, raw u16 (0 = fastest cadence field unused)
    on: bool
    reserved: int = 0

    @property
    def effect_name(self) -> str:
        return LED_EFFECT_NAMES.get(self.effect, f"0x{self.effect:02x}")


def parse_led_record(data: bytes) -> LedRecord:
    """Decode a 9-byte LED channel record (from a scene write or RDM PID 0x8000)."""
    if len(data) != _LED_RECORD_LEN:
        raise ValueError("a LED record is exactly 9 bytes")
    return LedRecord(
        red=data[0],
        green=data[1],
        blue=data[2],
        brightness=data[3],
        effect=data[4],
        period=struct.unpack_from(">H", data, 5)[0],
        reserved=data[7],
        on=data[8] == 0xFF,
    )


def build_led_record(record: LedRecord) -> bytes:
    """Encode a :class:`LedRecord` back into its 9 wire bytes."""
    buf = bytearray(_LED_RECORD_LEN)
    buf[0] = record.red & 0xFF
    buf[1] = record.green & 0xFF
    buf[2] = record.blue & 0xFF
    buf[3] = record.brightness & 0xFF
    buf[4] = int(record.effect) & 0xFF
    struct.pack_into(">H", buf, 5, record.period & 0xFFFF)
    buf[7] = record.reserved & 0xFF
    buf[8] = 0xFF if record.on else 0x00
    return bytes(buf)


def build_led_set_payload(channel: int, record: bytes) -> bytes:
    """Build the SET_LIVE_SCENE payload for one LED channel (1-3).

    Mirrors the app's captured RGB writes:
    ``05 01 <channel> 00×6 47 09 <9-byte record>``.
    """
    if not 1 <= int(channel) <= 3:
        raise ValueError("LED channel must be 1..3")
    if len(record) != _LED_RECORD_LEN:
        raise ValueError("a LED record is exactly 9 bytes")
    buf = bytearray(11)
    buf[0] = _LED_SCENE_ID
    buf[1] = _LED_SUB_ID
    buf[2] = int(channel)
    buf[9] = _LED_SCENE_TYPE
    buf[10] = _LED_RECORD_LEN
    return bytes(buf) + bytes(record)


def set_led_packet(channel: int, record: bytes, transaction_number: int = 0) -> bytes:
    """Full SET_LIVE_SCENE frame to drive one RGB channel."""
    return encode_packet(
        PacketType.SET_LIVE_SCENE,
        build_led_set_payload(channel, record),
        transaction_number,
    )


# Effect speed: the ``period`` field is a raw u16 that the app maps to a 0-100%
# slider on an exponential curve. Fitted from captured slider positions
# (24%->7997, 50%->793, 60%->335, 76%->81, 79%->64): period ranges from 65535 at
# 0% down to ~10 at 100%. Larger period = slower effect.
_SPEED_PERIOD_BASE = 65535
_SPEED_PERIOD_MIN = 10
_SPEED_K = -0.087944  # ln-slope per percent


def led_speed_to_period(percent: float) -> int:
    """Convert a 0-100% effect-speed slider to the raw ``period`` value."""
    percent = max(0.0, min(100.0, float(percent)))
    period = round(_SPEED_PERIOD_BASE * math.exp(_SPEED_K * percent))
    return max(_SPEED_PERIOD_MIN, min(_SPEED_PERIOD_BASE, period))


def led_period_to_speed(period: int) -> int:
    """Convert a raw ``period`` value back to the 0-100% speed the app shows."""
    period = max(_SPEED_PERIOD_MIN, min(_SPEED_PERIOD_BASE, int(period) or _SPEED_PERIOD_BASE))
    return max(0, min(100, round(math.log(period / _SPEED_PERIOD_BASE) / _SPEED_K)))


def parse_live_scene_reply(payload: bytes) -> LiveSceneReply:
    """Decode a GET_LIVE_SCENE reply payload. Mirrors parseLiveSceneReply."""
    if len(payload) < 11:
        raise ValueError("invalid live-scene reply length")
    scene_len = payload[10]
    return LiveSceneReply(
        type=payload[0],
        id=struct.unpack_from("<I", payload, 1)[0],
        count=struct.unpack_from("<I", payload, 5)[0],
        scene_type=payload[9],
        scene_length=scene_len,
        data=payload[11 : 11 + scene_len],
    )


def parse_socket_state(scene_data: bytes) -> SocketState:
    """Decode the 5-byte inner scene data of a GET reply. Mirrors parseSocketSceneGetReply."""
    if len(scene_data) != 5:
        raise ValueError("invalid socket scene length")
    return SocketState(
        socket1=scene_data[0] == 0xFF,
        socket2=scene_data[1] == 0xFF,
        socket3=scene_data[2] == 0xFF,
        dimmer_on=scene_data[3] == 0xFF,
        dimmer_value=scene_data[4],
    )


def parse_set_reply(payload: bytes) -> bool:
    """Return True if a SET_LIVE_SCENE reply indicates success.

    Mirrors ``parseSetLiveSceneReply``. Note the device answers ``0x01`` even for
    a socket index it does not implement, so a ``True`` here means "the packet was
    accepted", not "an outlet changed". Verified live against an FM-Master EGC Cloud.
    """
    if len(payload) < 1:
        raise ValueError("invalid set reply length")
    return payload[0] == 1


def device_table_packet(slot: int, transaction_number: int = 0) -> bytes:
    """Full DEVICE_TABLE request frame for one slot (0..11).

    The payload is the slot index followed by five zero bytes, as observed in the
    cloud's cached ``incrementStates`` requests.
    """
    if not 0 <= int(slot) <= 0xFF:
        raise ValueError("slot out of range")
    return encode_packet(
        PacketType.DEVICE_TABLE, bytes([int(slot)]) + b"\0" * 5, transaction_number
    )


def _ascii_z(data: bytes) -> str:
    """Decode a null-padded ASCII field, stopping at the first NUL."""
    return data.split(b"\0", 1)[0].decode("ascii", "replace").strip()


def parse_discovery_reply(payload: bytes) -> DiscoveryInfo:
    """Decode a ``DEVICE_INFO``/``DISCOVERY`` reply payload (324 bytes).

    String offsets are those of the reference ``parseDiscoveryReply``; they were
    re-verified byte-for-byte against a live FM-Master EGC Cloud. The two MAC
    addresses at offsets 134/140 are specific to the Cloud (ESP) variant: they
    carry an Espressif OUI and differ only in the last octet (an ESP32 STA/AP
    pair), so which one is the station interface is not established.
    """
    if len(payload) < _DISCOVERY_LEN:
        raise ValueError("invalid discovery reply length")
    macs = tuple(
        ":".join(f"{b:02x}" for b in payload[off : off + 6])
        for off in (_DISCOVERY_MAC1, _DISCOVERY_MAC2)
        if any(payload[off : off + 6])
    )
    return DiscoveryInfo(
        hw_type=payload[0],
        device_index=payload[1],
        name=_ascii_z(payload[2:34]),
        serial_number=_ascii_z(payload[34:46]),
        long_name=_ascii_z(payload[66:130]),
        mac_addresses=macs,
    )


def parse_device_table_entry(payload: bytes) -> DeviceTableEntry | None:
    """Decode one ``DEVICE_TABLE`` reply slot, or None if the slot is empty.

    Layout (verified live: article/device numbers cross-check against the values
    the cloud reports independently in the inventory's ``devices`` list)::

        0   u8    slot index
        4   u32LE internal handle (sequential per slot)
        8   u32LE article number
        12  u32LE device number
        16  2     magic "AO"
        20  u16LE device type code
        24  N     null-terminated ASCII product name

    An unused slot reports article number 0 and is filled with ``0xFF``.
    """
    if len(payload) < _DEVICE_TABLE_NAME:
        raise ValueError("invalid device table entry length")
    article_number = struct.unpack_from("<I", payload, 8)[0]
    if article_number == 0:
        return None
    return DeviceTableEntry(
        slot=payload[0],
        article_number=article_number,
        device_number=struct.unpack_from("<I", payload, 12)[0],
        name=_ascii_z(payload[_DEVICE_TABLE_NAME:]),
        type_code=struct.unpack_from("<H", payload, 20)[0],
    )


def encode_password_payload(password: str, *, unicode_encoded: bool = False) -> bytes:
    """Encode the device password into the 64-byte PASSWORD_CHECK payload.

    Mirrors ``get64BytesFromString``: optionally decode ``\\uXXXX`` escapes, UTF-8
    encode, then left-align into a zero-filled 64-byte buffer (truncating to 64).
    The 74-character credential from the cloud inventory is *not* unicode-encoded.
    """
    text = password
    if not unicode_encoded:
        import re

        text = re.sub(r"\\u([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), text)
    raw = text.encode("utf-8")[:64]
    return raw.ljust(64, b"\x00")


def password_check_packet(
    password: str, *, unicode_encoded: bool = False, transaction_number: int = 0
) -> bytes:
    """Full PASSWORD_CHECK frame (used only on the Phase-2 local transport)."""
    return encode_packet(
        PacketType.PASSWORD_CHECK,
        encode_password_payload(password, unicode_encoded=unicode_encoded),
        transaction_number,
    )
