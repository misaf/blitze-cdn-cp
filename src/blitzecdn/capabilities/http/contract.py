"""The HTTP capability's configuration contract.

HTTP/1.1 and HTTP/2 are edge invariants with no policy. The HTTP/3 field lives
here even though the ``http3`` slice implements it, so a site asking for it
loads anywhere and is refused by name where it cannot be served. The body size
limit uses a core nginx directive and requests nothing.
"""

from collections.abc import Mapping
from enum import StrEnum

from pydantic import ConfigDict, Field

from blitzecdn.core.domain.policy import CapabilityPolicy


class HttpScheme(StrEnum):
    HTTP = "http"
    HTTPS = "https"


# Cloudflare-compatible public proxy ports. These mirror the edge runtime
# listener contract and are intentionally independent sets rather than pairs.
HTTP_PROXY_PORTS = (80, 8080, 8880, 2052, 2082, 2086, 2095)
HTTPS_PROXY_PORTS = (443, 2053, 2083, 2087, 2096, 8443)
DEFAULT_PORTS = {HttpScheme.HTTP: 80, HttpScheme.HTTPS: 443}


class MaxUploadSize(StrEnum):
    """Largest visitor request body one site accepts, as an nginx size.

    A closed set, so API, CLI and role reject unknown values alike. Every tier is
    above nginx's 1m default.
    """

    SMALL = "100m"
    LARGE = "200m"


class ProtocolPolicy(CapabilityPolicy):
    """HTTP protocol behavior requested by one site."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # QUIC always negotiates TLS 1.3. This does not change the minimum TLS
    # version accepted by the site's parallel TCP listeners.
    http3_enabled: bool = Field(
        default=False, description="Offer visitor HTTP/3 over QUIC on UDP/443."
    )

    max_upload_size: MaxUploadSize = Field(
        default=MaxUploadSize.SMALL,
        description=(
            "Largest visitor request body this site accepts. A larger body is "
            "refused at the edge with 413 before the origin is contacted."
        ),
    )

    @property
    def capability_requirements(self) -> Mapping[str, tuple[str, ...]]:
        """``http3`` when HTTP/3 is enabled; nothing for HTTP/1.1 and HTTP/2."""
        if not self.http3_enabled:
            return {}
        return {"http3": ("http3_enabled",)}
