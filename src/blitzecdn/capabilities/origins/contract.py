"""Origin request identity policy for a site; every edge supports it."""

from pydantic import ConfigDict, field_validator

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
