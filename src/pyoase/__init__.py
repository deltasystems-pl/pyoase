"""pyoase — async client + O-Net codec for OASE InScenio FM-Master (EGC) devices.

Public API:
    OaseAuth           - Azure AD B2C login (email + password), token refresh   [needs aiohttp]
    OaseCloudClient    - read inventory, control sockets via the cloud O-Net relay  [needs aiohttp]
    Inventory, Gateway, Device, SocketsState, PumpState, User - typed models
    onet               - low-level O-Net packet codec (stdlib only)
    exceptions         - OaseError hierarchy

``onet`` and the models are stdlib-only and import without aiohttp; ``OaseAuth`` /
``OaseCloudClient`` are imported lazily so the codec can be used on its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import onet
from .exceptions import (
    OaseAuthError,
    OaseConnectionError,
    OaseError,
    OaseGatewayOfflineError,
    OaseResponseError,
)
from .models import Device, Gateway, Inventory, PumpState, SocketsState, User

__version__ = "0.1.2"

if TYPE_CHECKING:
    from .auth import OaseAuth
    from .client import OaseCloudClient

__all__ = [
    "OaseAuth",
    "OaseCloudClient",
    "Inventory",
    "Gateway",
    "Device",
    "SocketsState",
    "PumpState",
    "User",
    "onet",
    "OaseError",
    "OaseAuthError",
    "OaseConnectionError",
    "OaseGatewayOfflineError",
    "OaseResponseError",
    "__version__",
]

_LAZY = {"OaseAuth": ".auth", "OaseCloudClient": ".client"}


def __getattr__(name: str):  # PEP 562: import aiohttp-dependent classes on demand
    if name in _LAZY:
        import importlib

        module = importlib.import_module(_LAZY[name], __name__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
