"""The HTTP capability's configuration contract.

The scheme, the public proxy port sets, and the one protocol switch a site
owns. ``sites`` composes :class:`ProtocolPolicy` into its flat policy;
:mod:`blitzecdn.features.http.plugin` turns the enabled sites into the fleet's
QUIC listener state.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class HttpScheme(StrEnum):
    HTTP = "http"
    HTTPS = "https"


# Cloudflare-compatible public proxy ports. These mirror the edge runtime
# listener contract and are intentionally independent sets rather than pairs.
HTTP_PROXY_PORTS = (80, 8080, 8880, 2052, 2082, 2086, 2095)
HTTPS_PROXY_PORTS = (443, 2053, 2083, 2087, 2096, 8443)
DEFAULT_PORTS = {HttpScheme.HTTP: 80, HttpScheme.HTTPS: 443}


class ProtocolPolicy(BaseModel):
    """HTTP protocol behavior requested by one site."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # QUIC always negotiates TLS 1.3. This does not change the minimum TLS
    # version accepted by the site's parallel TCP listeners.
    http3_enabled: bool = False
