"""Typed data models for the OASE cloud inventory.

These mirror the shape actually returned by ``GET /User/Inventory`` on API v5.0. Parsing
is deliberately tolerant: fields that are null/absent become ``None`` or sensible
defaults, because the cloud omits state the device hasn't reported yet.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from typing import Any

from . import onet

_LOGGER = logging.getLogger(__name__)


def _increment_replies(d: dict, key: str) -> list[bytes]:
    """Return the cached O-Net reply payloads for one ``incrementStates`` key.

    The cloud keeps a verbatim cache of request/reply packet pairs per subsystem
    (``Name``, ``DeviceTable``, ...). The replies are bare payloads — no O-Net
    header — and are present even while the gateway is offline, so reading them
    costs no relay round-trip.
    """
    replies: list[bytes] = []
    for entry in d.get("incrementStates") or []:
        if entry.get("key") != key:
            continue
        for item in _get(entry, "value", "data", default=[]) or []:
            raw = item.get("reply")
            if not raw:
                continue
            try:
                replies.append(base64.b64decode(raw))
            except (ValueError, TypeError):
                _LOGGER.debug("undecodable %s reply in incrementStates", key)
    return replies


def _get(d: Any, *keys: str, default: Any = None) -> Any:
    """Safely walk nested dicts: _get(obj, 'onlineState', 'isOnline')."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return default if cur is None else cur


def _as_int(value: Any, default: int = 0) -> int:
    """Normalise a dimmer value that may be a plain int or an OnetByte dict."""
    if isinstance(value, dict):
        value = value.get("onetValue", value.get("intPercentage"))
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class SocketsState:
    """State of an FM-Master's four outlets (3 on/off + 1 dimmable)."""

    socket1: bool = False
    socket2: bool = False
    socket3: bool = False
    dimmer_on: bool = False
    dimmer_value: int = 0  # 0-255
    timestamp: str | None = None

    @classmethod
    def from_dict(cls, wrapper: dict | None) -> SocketsState | None:
        if not wrapper:
            return None
        v = wrapper.get("value") or {}
        return cls(
            socket1=bool(v.get("socket1")),
            socket2=bool(v.get("socket2")),
            socket3=bool(v.get("socket3")),
            dimmer_on=bool(v.get("socketDimmer")),
            dimmer_value=_as_int(v.get("dimmerValue")),
            timestamp=wrapper.get("timestamp"),
        )


@dataclass(frozen=True)
class PumpState:
    """DMX pump state of an attached device."""

    device_on: bool = False
    dimmer_value: int = 0
    fc_mode: int | None = None
    fc_status: str | None = None
    timestamp: str | None = None

    @property
    def show_active(self) -> bool:
        """True when a flow-control show is running (``fcStatus`` == DfcOn)."""
        return (self.fc_status or "").lower() == "dfcon"

    @classmethod
    def from_dict(cls, wrapper: dict | None) -> PumpState | None:
        if not wrapper:
            return None
        v = wrapper.get("value") or {}
        return cls(
            device_on=bool(v.get("deviceOn")),
            dimmer_value=_as_int(v.get("dimmerValue")),
            fc_mode=v.get("fcMode"),
            fc_status=v.get("fcStatus"),
            timestamp=wrapper.get("timestamp"),
        )


@dataclass(frozen=True)
class Device:
    """An OASE device attached to a gateway (pump, LED, filter, ...)."""

    id: str | None
    device_number: int | None
    article_number: int | None
    device_type: str
    is_connected: bool
    is_active: bool
    pump_state: PumpState | None
    has_rdm: bool
    custom_attributes: str | None
    #: OASE product name from the gateway's device table (e.g. "Expert 22000").
    product_name: str | None = None
    #: RGB channel records for an LED controller; filled in live over RDM by the
    #: coordinator (empty from a plain inventory parse).
    led_channels: tuple[onet.LedRecord, ...] = ()
    #: Operating-hours counter, read over RDM (None until the coordinator fills it).
    operating_hours: int | None = None
    #: Software/firmware version label, read over RDM.
    software_version: str | None = None

    @property
    def is_led(self) -> bool:
        """True for RGB/LED controllers (which expose ``led_channels``)."""
        return self.device_type.endswith("Led")

    @classmethod
    def from_dict(cls, d: dict, product_name: str | None = None) -> Device:
        return cls(
            id=d.get("id"),
            device_number=d.get("deviceNumber"),
            article_number=d.get("articleNumber"),
            device_type=d.get("deviceType") or "Unknown",
            is_connected=bool(_get(d, "connectionState", "isConnected")),
            is_active=bool(d.get("isActive")),
            pump_state=PumpState.from_dict(d.get("dmxPumpState")),
            has_rdm=bool(d.get("rdmData")),
            custom_attributes=d.get("customAttributesJson"),
            product_name=product_name,
        )


@dataclass(frozen=True)
class Gateway:
    """A gateway / control unit (e.g. an FM-Master Cloud)."""

    id: str
    serial_number: str | None
    article_number: int | None
    gateway_type: str
    is_online: bool
    online_event_time: str | None
    sockets: SocketsState | None
    devices: list[Device] = field(default_factory=list)
    custom_attributes: str | None = None
    #: Identity decoded from the cached DEVICE_INFO reply, when the cloud has one.
    info: onet.DiscoveryInfo | None = None

    @classmethod
    def from_dict(cls, d: dict) -> Gateway:
        product_names = cls._parse_device_table(d)
        return cls(
            id=d["id"],
            serial_number=d.get("serialNumber"),
            article_number=d.get("articleNumber"),
            gateway_type=d.get("gatewayType") or "Invalid",
            is_online=bool(d.get("isOnline")),
            online_event_time=_get(d, "onlineState", "eventTime"),
            sockets=SocketsState.from_dict(d.get("socketsState")),
            devices=[
                Device.from_dict(x, product_names.get(x.get("deviceNumber")))
                for x in (d.get("devices") or [])
            ],
            custom_attributes=d.get("customAttributesJson"),
            info=cls._parse_info(d),
        )

    @staticmethod
    def _parse_info(d: dict) -> onet.DiscoveryInfo | None:
        """Decode the cached DEVICE_INFO reply, if present and well-formed."""
        for payload in _increment_replies(d, "Name"):
            try:
                return onet.parse_discovery_reply(payload)
            except ValueError:
                _LOGGER.debug("unparsable DEVICE_INFO reply in incrementStates")
        return None

    @staticmethod
    def _parse_device_table(d: dict) -> dict[int, str]:
        """Map device number -> OASE product name from the cached device table."""
        names: dict[int, str] = {}
        for payload in _increment_replies(d, "DeviceTable"):
            try:
                entry = onet.parse_device_table_entry(payload)
            except ValueError:
                _LOGGER.debug("unparsable DeviceTable entry in incrementStates")
                continue
            if entry and entry.name:
                names[entry.device_number] = entry.name
        return names

    @property
    def is_fm_master(self) -> bool:
        from .const import FM_MASTER_TYPES

        return self.gateway_type in FM_MASTER_TYPES


@dataclass(frozen=True)
class User:
    """The account owner (minimal; PII kept out of logs/diagnostics)."""

    given_name: str | None
    surname: str | None
    culture: str | None

    @classmethod
    def from_dict(cls, d: dict | None) -> User | None:
        if not d:
            return None
        return cls(
            given_name=d.get("givenName"),
            surname=d.get("surname"),
            culture=d.get("cultureInfo"),
        )


@dataclass(frozen=True)
class Inventory:
    """Top-level ``GET /User/Inventory`` response."""

    user: User | None
    gateways: list[Gateway]

    @classmethod
    def from_dict(cls, d: dict) -> Inventory:
        return cls(
            user=User.from_dict(d.get("user")),
            gateways=[Gateway.from_dict(g) for g in (d.get("gateways") or [])],
        )

    def gateway(self, gateway_id: str) -> Gateway | None:
        return next((g for g in self.gateways if g.id == gateway_id), None)
