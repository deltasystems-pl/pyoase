"""Constants for the OASE cloud API and its Azure AD B2C authentication.

All values here are public: the ``client_id`` is a public OAuth client baked into the
OASE Control mobile app (MSAL.Xamarin.Android), used with PKCE and no secret.
"""

from __future__ import annotations

# --- Cloud REST API -------------------------------------------------------------
API_BASE = "https://app-oasecloud-prod.azurewebsites.net"
API_VERSION = "5.0"
DEFAULT_TIMEOUT = 30
#: Seconds the cloud waits for the gateway to answer a relayed O-Net packet.
ONET_RELAY_TIMEOUT = 10

# --- Azure AD B2C ---------------------------------------------------------------
B2C_TENANT = "oasecustomersprod.onmicrosoft.com"
B2C_POLICY = "B2C_1A_SignUp_SignIn"
B2C_BASE = f"https://account.oase.com/{B2C_TENANT}/{B2C_POLICY}"
B2C_AUTHORIZE_URL = f"{B2C_BASE}/oauth2/v2.0/authorize"
B2C_TOKEN_URL = f"{B2C_BASE}/oauth2/v2.0/token"
B2C_SELF_ASSERTED_URL = f"{B2C_BASE}/SelfAsserted"
B2C_CONFIRMED_URL = f"{B2C_BASE}/api/CombinedSigninAndSignup/confirmed"

#: Public OAuth client id of the OASE Control app (from its authorize URL).
CLIENT_ID = "8dfe4495-b83f-4e4f-861c-83b6b3cbaa3b"
REDIRECT_URI = f"msal{CLIENT_ID}://auth"
SCOPES = (
    "https://oasecustomersprod.onmicrosoft.com/api/oase.read "
    "https://oasecustomersprod.onmicrosoft.com/api/oase.readwrite "
    "openid profile offline_access"
)

#: User-Agent mirroring the mobile client (some B2C flows are picky about this).
USER_AGENT = "Mozilla/5.0 (Linux; Android 13; SM-S911B) AppleWebKit/537.36 Chrome/125.0 Mobile Safari/537.36"

# --- Device model ---------------------------------------------------------------
#: Gateway ``gatewayType`` values that are FM-Master socket controllers.
FM_MASTER_TYPES = frozenset(
    {
        "FmMasterWLanGatewayOld",
        "FmMasterWLanEgcCloud",
        "FmMasterWLanEgcHome",
        "FmMasterWLanEgcCloudEsp",
        "FmMasterWLanEgcHomeEsp",
        "FmMasterWLanEgcCloudStaging",
    }
)
