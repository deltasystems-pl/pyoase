"""Exception hierarchy for pyoase."""

from __future__ import annotations


class OaseError(Exception):
    """Base class for all pyoase errors."""


class OaseAuthError(OaseError):
    """Authentication failed (bad credentials, expired/invalid refresh token).

    The Home Assistant integration maps this to a reauth flow.
    """


class OaseConnectionError(OaseError):
    """A transient network/HTTP problem talking to the OASE cloud."""


class OaseGatewayOfflineError(OaseError):
    """The target gateway is not currently connected to the cloud.

    Raised when a relayed O-Net command times out (HTTP 504) or the gateway is
    reported offline — the device is likely powered off or off the network.
    """


class OaseResponseError(OaseError):
    """The cloud returned an unexpected or malformed response."""
