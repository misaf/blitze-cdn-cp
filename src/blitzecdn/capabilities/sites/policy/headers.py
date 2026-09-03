"""Visitor metadata headers written by the edge for an origin."""

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

from blitzecdn.core.policy import CapabilityPolicy


class SiteVisitorHeaders(BaseModel):
    """The trusted ``BZ-*`` request headers the edge writes to an origin."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    connecting_ip: bool = True
    ip_country: bool = False

    @property
    def requires_geoip(self) -> bool:
        return self.ip_country


class HeaderPolicy(CapabilityPolicy):
    """Visitor header behavior requested by one site."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Replaced wholesale when patched so disabling the last switch is explicit.
    visitor_headers: SiteVisitorHeaders = SiteVisitorHeaders()

    @property
    def capability_requirements(self) -> Mapping[str, tuple[str, ...]]:
        """Implementation capabilities requested by this stable policy.

        Writing the headers is the managed edge's own job and asks for nothing.
        ``BZ-IPCountry`` is the exception: it carries a value only a GeoIP
        lookup can produce, so the header a site turns on is what requests the
        capability — the same token a country firewall rule requests, from the
        other contract that needs it.
        """
        if not self.visitor_headers.requires_geoip:
            return {}
        return {"geoip": ("visitor_headers.ip_country",)}
