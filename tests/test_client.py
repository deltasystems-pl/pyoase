"""Tests for :class:`pyoase.client.OaseCloudClient` request shaping.

The HTTP layer is stubbed at ``_request``: these assert *what* the client asks
the cloud for and how it hands the answer back, not how aiohttp behaves.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

from pyoase.client import OaseCloudClient

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "inventory.json"
_INVENTORY_CALL = ("GET", "/User/Inventory")


def _client() -> OaseCloudClient:
    return OaseCloudClient(MagicMock(), MagicMock())


def test_get_inventory_raw_returns_the_payload_untouched():
    payload = json.loads(FIXTURE.read_text())
    with patch.object(
        OaseCloudClient, "_request", AsyncMock(return_value=payload)
    ) as request:
        assert asyncio.run(_client().async_get_inventory_raw()) is payload
    request.assert_awaited_once_with(
        *_INVENTORY_CALL, params={"onlyOwnedGateways": "false"}
    )


def test_get_inventory_parses_the_same_request():
    payload = json.loads(FIXTURE.read_text())
    with patch.object(
        OaseCloudClient, "_request", AsyncMock(return_value=payload)
    ) as request:
        inventory = asyncio.run(_client().async_get_inventory())
    request.assert_awaited_once_with(
        *_INVENTORY_CALL, params={"onlyOwnedGateways": "false"}
    )
    assert inventory.gateways[0].gateway_type == "FmMasterWLanEgcCloudEsp"
