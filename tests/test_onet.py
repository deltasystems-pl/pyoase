"""Golden-vector tests for the O-Net codec.

The expected byte strings are derived from the reference MIT implementation
(ioBroker.oasecontrol ``lib/oase/protocol.js`` + ``index.js``). If these pass,
the exact bytes we send through the cloud ``SendONetPacket`` relay — or a local
TLS socket — match what a proven, real-hardware adapter sends.
"""

from __future__ import annotations

import struct

import pytest

from pyoase import onet, rdm


def test_socket1_on_golden():
    pkt = onet.set_socket_packet(onet.Socket.SOCKET_1, onet.ON, transaction_number=0)
    assert pkt.hex() == "5c234f410d000000020000c400000000040000000000000000640200ff"


def test_socket3_off_golden_with_txn():
    pkt = onet.set_socket_packet(onet.Socket.SOCKET_3, onet.OFF, transaction_number=7)
    # txn byte (offset 9) is 07; socketIdx 02, value 00
    assert pkt.hex() == "5c234f410d000000020700c40000000004000000000000000064020200"


def test_dimmer_level_golden():
    pkt = onet.set_socket_packet(onet.Socket.DIMMER_VALUE, 128, transaction_number=0)
    assert pkt.hex() == "5c234f410d000000020000c40000000004000000000000000064020480"


def test_get_scene_golden():
    assert onet.get_scene_packet(0).hex() == "5c234f4105000000020000c5000000000400000000"


def test_header_fields():
    pkt = onet.set_socket_packet(onet.Socket.SOCKET_2, onet.ON, transaction_number=42)
    assert pkt[0:4] == onet.START_DELIMITER
    assert struct.unpack_from("<I", pkt, 4)[0] == 13  # payload length
    assert pkt[8] == onet.PROTOCOL_VERSION
    assert pkt[9] == 42
    assert struct.unpack_from("<H", pkt, 10)[0] == onet.PacketType.SET_LIVE_SCENE


def test_set_payload_structure():
    payload = onet.build_socket_set_payload(onet.Socket.DIMMER_ONOFF, onet.ON)
    assert len(payload) == 13
    assert payload[0] == 4  # SceneId
    assert payload[9] == 0x64  # SceneType
    assert payload[10] == 2  # SceneLength
    assert payload[11] == 0x03  # DimmerOnOff index
    assert payload[12] == 0xFF


def test_encode_parse_roundtrip():
    pkt = onet.set_socket_packet(onet.Socket.SOCKET_1, onet.ON, transaction_number=5)
    decoded = onet.parse_packet(pkt)
    assert decoded.packet_type == onet.PacketType.SET_LIVE_SCENE
    assert decoded.transaction_number == 5
    assert decoded.payload == onet.build_socket_set_payload(0, 0xFF)


def test_parse_socket_state():
    inner = bytes([0xFF, 0x00, 0xFF, 0xFF, 0x80])
    reply = bytes([1]) + struct.pack("<I", 0) + struct.pack("<I", 1) + bytes([100, 5]) + inner
    lsr = onet.parse_live_scene_reply(reply)
    assert lsr.scene_length == 5
    state = onet.parse_socket_state(lsr.data)
    assert state == onet.SocketState(
        socket1=True, socket2=False, socket3=True, dimmer_on=True, dimmer_value=128
    )


def test_parse_set_reply():
    assert onet.parse_set_reply(bytes([1])) is True
    assert onet.parse_set_reply(bytes([0])) is False


def test_bad_delimiter_rejected():
    with pytest.raises(ValueError):
        onet.parse_packet(b"\x00\x00\x00\x00" + b"\x00" * 16)


def test_password_payload_truncates_to_64_bytes():
    payload = onet.encode_password_payload("x" * 74)
    assert len(payload) == 64
    assert payload == b"x" * 64  # 74 chars truncated to 64, no room for padding


def test_password_payload_zero_pads_short_input():
    payload = onet.encode_password_payload("abc")
    assert len(payload) == 64
    assert payload == b"abc" + b"\x00" * 61


def test_password_unicode_escape_decoded():
    # "A" -> "A" when not already unicode-encoded
    payload = onet.encode_password_payload(r"ABC", unicode_encoded=False)
    assert payload[:3] == b"ABC"


# --- reply typing ---------------------------------------------------------


def test_reply_type_sets_low_byte_to_ff():
    # Verified live for every request/reply pair the cloud caches in incrementStates.
    assert onet.reply_type(onet.PacketType.GET_LIVE_SCENE) == 0xC5FF
    assert onet.reply_type(onet.PacketType.SET_LIVE_SCENE) == 0xC4FF
    assert onet.reply_type(onet.PacketType.DEVICE_INFO) == 0x10FF
    assert onet.reply_type(onet.PacketType.DEVICE_TABLE) == 0x40FF
    assert onet.reply_type(0xB300) == 0xB3FF


# --- DEVICE_INFO / DISCOVERY ---------------------------------------------


def _synthetic_discovery() -> bytes:
    """Build a 324-byte discovery payload with the live field layout, fake ids."""
    buf = bytearray(324)
    buf[0] = 8  # hwType
    buf[1] = 0  # devIdx
    buf[2:2 + 15] = b"FM-Master Cloud"
    buf[34:34 + 12] = b"000000000000"
    buf[66:66 + 19] = b"FM-Master EGC Cloud"
    buf[134:140] = bytes.fromhex("aabbccddee01")
    buf[140:146] = bytes.fromhex("aabbccddee02")
    return bytes(buf)


def test_parse_discovery_reply_fields():
    info = onet.parse_discovery_reply(_synthetic_discovery())
    assert info.hw_type == 8
    assert info.device_index == 0
    assert info.name == "FM-Master Cloud"
    assert info.serial_number == "000000000000"
    assert info.long_name == "FM-Master EGC Cloud"
    assert info.mac_addresses == ("aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02")


def test_parse_discovery_reply_omits_absent_second_mac():
    # The cloud's cached copy can predate the AP interface coming up.
    payload = bytearray(_synthetic_discovery())
    payload[140:146] = b"\x00" * 6
    info = onet.parse_discovery_reply(bytes(payload))
    assert info.mac_addresses == ("aa:bb:cc:dd:ee:01",)


def test_parse_discovery_reply_rejects_short_payload():
    with pytest.raises(ValueError):
        onet.parse_discovery_reply(b"\x00" * 323)


# --- DEVICE_TABLE ---------------------------------------------------------


def _synthetic_table_entry(slot: int, article: int, number: int, name: bytes) -> bytes:
    buf = bytearray(76)
    buf[0] = slot
    struct.pack_into("<I", buf, 4, 0x0005B4C0 + slot)
    struct.pack_into("<I", buf, 8, article)
    struct.pack_into("<I", buf, 12, number)
    buf[16:18] = b"AO"
    struct.pack_into("<H", buf, 20, 33)
    buf[24:24 + len(name)] = name
    return bytes(buf)


def test_parse_device_table_entry_populated():
    entry = onet.parse_device_table_entry(
        _synthetic_table_entry(0, 12345, 999000111, b"Expert 22000")
    )
    assert entry is not None
    assert entry.slot == 0
    assert entry.article_number == 12345
    assert entry.device_number == 999000111
    assert entry.name == "Expert 22000"
    assert entry.type_code == 33


def test_parse_device_table_entry_empty_slot_is_none():
    # Unused slots report article number 0 and are 0xFF-filled.
    buf = bytearray(b"\xff" * 76)
    buf[0] = 5
    struct.pack_into("<I", buf, 8, 0)
    assert onet.parse_device_table_entry(bytes(buf)) is None


def test_device_table_packet_round_trip():
    packet = onet.device_table_packet(3)
    decoded = onet.parse_packet(packet)
    assert decoded.packet_type == onet.PacketType.DEVICE_TABLE
    assert decoded.payload == b"\x03\x00\x00\x00\x00\x00"


# --- RDM / EGC ------------------------------------------------------------


def test_uid_round_trip_and_format():
    u = rdm.Uid.for_device(1000000001)
    assert u.manufacturer == rdm.OASE_MANUFACTURER_ID
    assert bytes(u) == bytes.fromhex("4F413B9ACA01")
    assert str(u) == "4F41:3B9ACA01"
    assert rdm.Uid.from_bytes(bytes(u)) == u


def test_uid_rejects_wrong_length():
    with pytest.raises(ValueError):
        rdm.Uid.from_bytes(b"\x00" * 5)


def test_build_frame_matches_live_verified_get():
    # The exact GET that returned the pump's on/off state live.
    frame = rdm.build_frame(
        rdm.Uid.for_device(1000000001), rdm.CommandClass.GET_COMMAND, rdm.Pid.DEVICE_ON
    )
    assert frame[0:2] == b"\xcc\x01"
    assert frame[2] == 24  # message length: 24 + PDL(0)
    assert frame[3:9] == bytes.fromhex("4F413B9ACA01")  # destination
    assert frame[9:15] == b"\x00" * 6  # source: null controller UID
    assert frame[20] == 0x20  # GET_COMMAND
    assert struct.unpack_from(">H", frame, 21)[0] == 0x1010
    assert frame[23] == 0  # PDL
    # trailing checksum covers everything before it
    assert struct.unpack_from(">H", frame, 24)[0] == sum(frame[:24]) & 0xFFFF


def test_build_frame_with_data_sets_pdl_and_length():
    frame = rdm.build_frame(
        rdm.Uid.for_device(1), rdm.CommandClass.SET_COMMAND, rdm.Pid.PUMP_POWER, b"\x39"
    )
    assert frame[2] == 25  # 24 + PDL(1)
    assert frame[23] == 1
    assert frame[24] == 0x39
    assert struct.unpack_from(">H", frame, 25)[0] == sum(frame[:25]) & 0xFFFF


def test_parse_frame_round_trips_a_response():
    # A response frame is the same layout; craft one and read it back.
    frame = bytearray(
        rdm.build_frame(
            rdm.Uid.for_device(7), rdm.CommandClass.GET_COMMAND_RESPONSE, rdm.Pid.DEVICE_ON,
            b"\xff",
        )
    )
    frame[16] = rdm.ResponseType.ACK
    frame[25:27] = struct.pack(">H", sum(frame[:25]) & 0xFFFF)  # fix checksum after edit
    # the real device zero-pads the reply to 257 bytes
    padded = bytes(frame) + b"\x00" * (257 - len(frame))
    r = rdm.parse_frame(padded)
    assert r.checksum_valid is True
    assert r.is_ack is True
    assert r.pid == rdm.Pid.DEVICE_ON
    assert r.data == b"\xff"


def test_parse_frame_rejects_non_rdm():
    with pytest.raises(ValueError):
        rdm.parse_frame(b"\x00" * 40)


def test_parse_frame_detects_bad_checksum():
    frame = bytearray(
        rdm.build_frame(rdm.Uid.for_device(7), rdm.CommandClass.GET_COMMAND, rdm.Pid.DEVICE_ON)
    )
    frame[-1] ^= 0xFF
    assert rdm.parse_frame(bytes(frame)).checksum_valid is False


def test_parse_discovery_reply_live_vector():
    # Captured verbatim from a live 0x70FF reply (2 devices: pump + RGB controller).
    payload = bytes.fromhex(
        "00000000" "02000000"
        "a5a50000" "01ca9a3b" "414f" "0000"
        "8fa60000" "02ca9a3b" "414f" "0300"
    )
    devices = rdm.parse_discovery_reply(payload)
    assert len(devices) == 2
    pump, led = devices
    assert (pump.article_number, pump.device_number) == (42405, 1000000001)
    assert pump.subdevice_count == 0
    assert str(pump.uid) == "4F41:3B9ACA01"
    assert (led.article_number, led.device_number) == (42639, 1000000002)
    assert led.subdevice_count == 3  # RGB 1/2/3


def test_parse_discovery_reply_tolerates_truncation():
    payload = bytes.fromhex("00000000" "05000000" "a5a5000001ca9a3b414f0000")
    assert len(rdm.parse_discovery_reply(payload)) == 1  # claims 5, only 1 intact


def test_percent_raw_conversions_match_the_app():
    # From the OASE app's own displayed values.
    assert rdm.percent_to_raw(1) == 0x02
    assert rdm.percent_to_raw(25) == 0x3F
    assert rdm.percent_to_raw(50) == 0x7F
    assert rdm.percent_to_raw(100) == 0xFF
    assert rdm.raw_to_percent(0x7F) == 50
    assert rdm.raw_to_percent(0x80) == 51
    assert rdm.raw_to_percent(0xFF) == 100
    # the user's live pump reading: raw 57 displayed as 23%
    assert rdm.raw_to_percent(57) == 23


def test_percent_to_raw_clamps():
    assert rdm.percent_to_raw(-10) == 0
    assert rdm.percent_to_raw(500) == 255


# --- LED / RGB scene (captured from the OASE app) -------------------------


def test_led_record_round_trips_captured_red():
    # RGB1 -> red capture: data bytes after the 11-byte scene header.
    data = bytes.fromhex("ff00007a00004000ff")
    rec = onet.parse_led_record(data)
    assert (rec.red, rec.green, rec.blue) == (255, 0, 0)
    assert rec.brightness == 0x7A
    assert rec.effect == onet.LedEffect.NONE
    assert rec.on is True
    assert onet.build_led_record(rec) == data


def test_led_set_payload_matches_app_capture():
    # Full captured RGB1->blue SET_LIVE_SCENE payload.
    import base64
    captured = onet.parse_packet(
        base64.b64decode("XCNPQRQAAAACEADEAAAAAAUBAQAAAAAAAEcJAAD/egAAQAD/")
    ).payload
    rec = onet.parse_led_record(captured[11:20])
    rebuilt = onet.build_led_set_payload(1, onet.build_led_record(rec))
    assert rebuilt == captured
    assert (rec.red, rec.green, rec.blue) == (0, 0, 255)


def test_led_effect_ids_match_pokaz_menu():
    # Verified from per-effect captures, in menu order.
    assert onet.LedEffect.NONE == 0x00
    assert onet.LedEffect.FIRE == 0x10
    assert onet.LedEffect.ICE == 0x1A
    assert onet.LedEffect.AURORA == 0x2E
    assert onet.LedEffect.RAINBOW == 0x4C
    assert onet.LED_EFFECT_NAMES[onet.LedEffect.AURORA] == "Aurora"


def test_led_set_payload_channel_and_length():
    payload = onet.build_led_set_payload(2, bytes(9))
    assert payload[0] == 5  # LED scene id
    assert payload[2] == 2  # channel
    assert payload[9] == 0x47  # LED scene type
    assert payload[10] == 9  # record length
    assert len(payload) == 20


def test_led_set_payload_rejects_bad_channel():
    with pytest.raises(ValueError):
        onet.build_led_set_payload(4, bytes(9))


def test_led_record_rejects_wrong_length():
    with pytest.raises(ValueError):
        onet.parse_led_record(bytes(8))


def test_led_speed_period_round_trip_and_bounds():
    assert onet.led_speed_to_period(0) == 65535
    assert onet.led_speed_to_period(100) == 10
    # captured sample points, within a couple percent
    assert abs(onet.led_speed_to_period(60) - 335) < 20
    assert abs(onet.led_speed_to_period(76) - 81) < 10
    # round-trips through the exponential mapping
    for pct in (0, 25, 50, 75, 100):
        assert onet.led_period_to_speed(onet.led_speed_to_period(pct)) == pct


def test_led_speed_clamps():
    assert onet.led_speed_to_period(-5) == 65535
    assert onet.led_speed_to_period(200) == 10
    assert onet.led_period_to_speed(0) == 0


def test_pump_show_names_cover_all_modes():
    assert onet.PUMP_SHOW_NAMES[0] == "Off"
    assert onet.PUMP_SHOW_NAMES[5] == "Splashy"
    assert set(onet.PUMP_SHOW_NAMES) == set(range(13))


def test_pump_show_packet_encoding():
    # Splashy (mode 5) — matches the captured app packet payload 00 00 00 00 01 05.
    pkt = onet.encode_packet(onet.PacketType.PUMP_SHOW, bytes([0, 0, 0, 0, 1, 5]))
    decoded = onet.parse_packet(pkt)
    assert decoded.packet_type == 0x5000
    assert decoded.payload == bytes([0, 0, 0, 0, 1, 5])


def test_parse_supported_parameters_decodes_big_endian_pids():
    data = bytes.fromhex("00500060008000c010108039")
    assert rdm.parse_supported_parameters(data) == (0x0050, 0x0060, 0x0080, 0x00C0, 0x1010, 0x8039)


def test_parse_supported_parameters_tolerates_empty_and_odd_replies():
    assert rdm.parse_supported_parameters(b"") == ()
    # A trailing odd byte is padding, not half a PID.
    assert rdm.parse_supported_parameters(bytes.fromhex("10108039ff")) == (0x1010, 0x8039)
