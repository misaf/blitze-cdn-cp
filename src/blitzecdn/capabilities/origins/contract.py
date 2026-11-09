"""Origin request identity policy for a site, and one edge's answer about it.

The policy is supported by every edge. ``OriginCheck`` is published here
because the edges' controller probe, used by certificate preflight, produces
it too.
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from blitzecdn.capabilities.http.contract import HttpScheme
from blitzecdn.capabilities.tls.contract import SslMode
from blitzecdn.core.domain.policy import CapabilityPolicy
from blitzecdn.core.domain.validation import hostname, is_private_origin
from blitzecdn.core.features.types import HostView, PolicyView


class OriginPolicy(CapabilityPolicy):
    """Host header and SNI overrides used on the origin leg, and where it may go."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    origin_request_host: str | None = None
    origin_sni: str | None = None
    #: Lets a host proxy to a private address (RFC 1918, shared address space,
    #: IPv6 unique local), which is reached from the edge's own network. Off
    #: by default: those are the provider's internal hosts, not a website,
    #: unless the operator says so for this zone or rule.
    allow_private_origin: bool = False

    @field_validator("origin_request_host", "origin_sni")
    @classmethod
    def validate_optional_host(cls, value: str | None) -> str | None:
        return hostname(value) if value is not None else None

    def effective_sni(self, origin_host: str) -> str:
        """Explicit SNI, then the request host, then the origin address.

        Host overrides are validated without wildcards: a literal wildcard
        does not name an origin certificate. The edge uses this same order.
        """
        return self.origin_sni or self.origin_request_host or origin_host


def private_origin_needs_opt_in(host: HostView, policy: PolicyView) -> None:
    """A private origin is served only where ``allow_private_origin`` says so.

    Loopback, link-local and the like are refused for every host, in core.
    """
    if policy[OriginPolicy].allow_private_origin or not is_private_origin(
        host.origin_host
    ):
        return
    raise ValueError(
        f"origin {host.origin_host} is a private address, reached from the "
        "edge's own network; set allow_private_origin on the zone or rule if "
        "this origin is meant to be internal"
    )


class OriginCheck(BaseModel):
    """One edge's answer about one site's origin.

    ``reachable`` false is a network problem; ``tls_verified`` false with
    ``reachable`` true is a certificate mismatch for the SNI sent; ``None`` when
    verification is not attempted. ``status`` is unjudged: any answer means the
    origin is up.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    site: str
    origin: str
    scheme: HttpScheme
    ssl_mode: SslMode
    sni: str | None = None
    reachable: bool = False
    tls_verified: bool | None = None
    #: The HTTP status the origin answered with, or ``None`` if it did not.
    status: int | None = None
    #: Present only for Automatic SSL/TLS scans, which compare the response
    #: reached over the current transport with the same response over HTTPS.
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.reachable and self.tls_verified is not False
