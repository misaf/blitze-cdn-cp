"""Visitor metadata headers written by the edge for an origin."""

from pydantic import BaseModel, ConfigDict


class SiteVisitorHeaders(BaseModel):
    """The trusted ``BZ-*`` request headers the edge writes to an origin."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    connecting_ip: bool = True
    ip_country: bool = False

    @property
    def requires_geoip(self) -> bool:
        return self.ip_country


class HeaderPolicy(BaseModel):
    """Visitor header behavior requested by one site."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Replaced wholesale when patched so disabling the last switch is explicit.
    visitor_headers: SiteVisitorHeaders = SiteVisitorHeaders()
