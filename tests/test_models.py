"""Tests for inventory model parsing against a realistic (synthetic) fixture."""

from __future__ import annotations

import json
import pathlib

from pyoase.models import Inventory

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "inventory.json"


def _inventory() -> Inventory:
    return Inventory.from_dict(json.loads(FIXTURE.read_text()))


def test_parses_gateway_and_sockets():
    inv = _inventory()
    assert len(inv.gateways) == 1
    gw = inv.gateways[0]
    assert gw.gateway_type == "FmMasterWLanEgcCloudEsp"
    assert gw.is_fm_master
    assert gw.is_online is True
    assert gw.sockets is not None
    assert (gw.sockets.socket1, gw.sockets.socket2, gw.sockets.socket3) == (True, False, True)
    assert gw.sockets.dimmer_on is True
    assert gw.sockets.dimmer_value == 84


def test_parses_attached_devices():
    gw = _inventory().gateways[0]
    types = {d.device_type for d in gw.devices}
    assert types == {"GardenPump", "GardenLed"}
    pump = next(d for d in gw.devices if d.device_type == "GardenPump")
    assert pump.is_connected is True
    assert pump.pump_state is not None
    assert pump.pump_state.device_on is True
    assert pump.pump_state.dimmer_value == 93
    assert pump.pump_state.fc_status == "DfcOff"
    led = next(d for d in gw.devices if d.device_type == "GardenLed")
    assert led.pump_state is None
    assert led.has_rdm is True


def test_gateway_lookup_by_id():
    inv = _inventory()
    gw = inv.gateways[0]
    assert inv.gateway(gw.id) is gw
    assert inv.gateway("does-not-exist") is None


def test_dimmer_value_accepts_onetbyte_dict():
    # the API sometimes returns dimmerValue as an OnetByte object instead of an int
    data = {
        "gateways": [
            {
                "id": "x",
                "gatewayType": "FmMasterWLanEgcCloud",
                "socketsState": {"value": {"dimmerValue": {"onetValue": 200}}},
            }
        ]
    }
    inv = Inventory.from_dict(data)
    assert inv.gateways[0].sockets.dimmer_value == 200


def test_tolerates_missing_fields():
    inv = Inventory.from_dict({"gateways": [{"id": "y", "gatewayType": "GatewayCloud"}]})
    gw = inv.gateways[0]
    assert gw.sockets is None
    assert gw.devices == []
    assert gw.is_online is False
