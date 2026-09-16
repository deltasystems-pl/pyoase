"""Tests for :class:`pyoase.client.OaseCloudClient` request shaping.

The HTTP layer is stubbed at ``_request``: these assert *what* the client asks
the cloud for and how it hands the answer back, not how aiohttp behaves.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pyoase
from pyoase.client import OaseCloudClient
from pyoase.exceptions import OaseResponseError

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


def test_get_supported_parameters_decodes_the_reply():
    data = bytes.fromhex("10108039")
    with patch.object(OaseCloudClient, "async_rdm_get", AsyncMock(return_value=data)):
        pids = asyncio.run(_client().async_get_supported_parameters("gw", 111111111))
    assert pids == (0x1010, 0x8039)


def test_get_supported_parameters_is_empty_when_unanswered():
    with patch.object(
        OaseCloudClient, "async_rdm_get", AsyncMock(side_effect=OaseResponseError("nack"))
    ):
        assert asyncio.run(_client().async_get_supported_parameters("gw", 1)) == ()


def test_version_is_in_step_with_the_package_metadata():
    # These drifted once (0.1.0 vs 0.1.1) and nothing noticed; __version__ is
    # what a bug report quotes, so it has to be the version that shipped.
    pyproject = pathlib.Path(__file__).parents[1] / "pyproject.toml"
    declared = next(
        line.split('"')[1]
        for line in pyproject.read_text().splitlines()
        if line.startswith("version = ")
    )
    assert pyoase.__version__ == declared
