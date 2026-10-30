"""Visitor metadata headers written by the edge for an origin."""

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from blitzecdn.core.domain.policy import CapabilityPolicy


class SiteVisitorHeaders(BaseModel):
    """The trusted ``BZ-*`` request headers the edge writes to an origin."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    connecting_ip: bool = Field(
        default=True,
        description=(
            "Send BZ-Connecting-IP, the address of the peer the edge accepted the "
            "connection from; never derived from a request header."
        ),
    )
    ip_country: bool = Field(
        default=False,
        description=(
            "Send BZ-IPCountry, the visitor's ISO 3166-1 alpha-2 country. Needs "
            "the geoip capability."
        ),
    )

    @property
    def requires_geoip(self) -> bool:
        return self.ip_country


class HeaderPolicy(CapabilityPolicy):
    """Visitor header behavior requested by one site."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Replaced wholesale when patched so disabling the last switch is explicit.
    visitor_headers: SiteVisitorHeaders = Field(
        default=SiteVisitorHeaders(),
        description=(
            "The BZ-* request headers the edge writes to the origin. Each "
            "overwrites what the visitor sent under that name, and one turned off "
            "is cleared, so the BZ- namespace never carries client input."
        ),
    )

    @property
    def capability_requirements(self) -> Mapping[str, tuple[str, ...]]:
        """``geoip`` when ``BZ-IPCountry`` is on; writing headers needs nothing else."""
        if not self.visitor_headers.requires_geoip:
            return {}
        return {"geoip": ("visitor_headers.ip_country",)}
