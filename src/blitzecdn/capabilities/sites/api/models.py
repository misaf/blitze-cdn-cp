"""The HTTP representations this capability publishes, and the bodies it takes.

A site's published shape is the flat projection of every capability's contract
onto one virtual host, exactly as `sites` composes it — which is why it belongs
here and not in a module shared with `dns` and `edges`. The field list below
and `sites/policy/` change together, and now they change in the same directory.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from blitzecdn.api.models import Model
from blitzecdn.capabilities.cache.policy import CacheQueryStringMode
from blitzecdn.capabilities.compression.policy import CompressionMode
from blitzecdn.capabilities.http.policy import MaxUploadSize
from blitzecdn.capabilities.sites.domain import CdnSite as DomainCdnSite
from blitzecdn.capabilities.sites.domain import SitePatch as DomainSitePatch
from blitzecdn.capabilities.tls.policy import (
    CertificateMode,
    MinimumTlsVersion,
    SslAutomaticMode,
    SslMode,
)


class SiteFirewall(Model):
    allow_sources: tuple[str, ...] = Field(default=(), max_length=200)
    deny_sources: tuple[str, ...] = Field(default=(), max_length=200)
    allowed_countries: tuple[str, ...] = Field(default=(), max_length=250)
    denied_countries: tuple[str, ...] = Field(default=(), max_length=250)
    denied_methods: tuple[str, ...] = Field(default=(), max_length=20)
    denied_paths: tuple[str, ...] = Field(default=(), max_length=100)


class SiteVisitorHeaders(Model):
    """The ``BZ-*`` headers the edge writes on the request to the origin."""

    connecting_ip: bool = True
    ip_country: bool = False


class SitePolicy(Model):
    ssl_mode: SslMode = SslMode.OFF
    ssl_automatic_mode: SslAutomaticMode = SslAutomaticMode.AUTO
    minimum_tls_version: MinimumTlsVersion = MinimumTlsVersion.TLS_1_2
    http3_enabled: bool = False
    max_upload_size: MaxUploadSize = MaxUploadSize.SMALL
    always_use_https: bool = False
    under_attack_mode: bool = False
    origin_request_host: str | None = None
    origin_sni: str | None = None
    enabled: bool = True
    certificate_mode: CertificateMode = CertificateMode.DISABLED
    certificate_path: str | None = None
    certificate_key_path: str | None = None
    cache_enabled: bool = True
    cache_query_string_mode: CacheQueryStringMode = CacheQueryStringMode.INCLUDE
    cache_valid_success: str = "10m"
    cache_valid_not_found: str = "1m"
    compression: CompressionMode = CompressionMode.BROTLI
    firewall: SiteFirewall = Field(default_factory=SiteFirewall)
    visitor_headers: SiteVisitorHeaders = Field(default_factory=SiteVisitorHeaders)


class CdnSite(SitePolicy):
    name: str
    server_names: tuple[str, ...]
    origin_host: str

    @classmethod
    def from_domain(cls, value: DomainCdnSite) -> Self:
        return cls.model_validate(value.model_dump(mode="json"))


class CdnSiteCreate(SitePolicy):
    """The body that creates a site.

    ``server_names`` is absent rather than optional: it is the set of hostnames
    whose records route here, so it appears on the representation that is read
    back and on no body that writes one.
    """

    name: str
    origin_host: str

    @model_validator(mode="after")
    def valid_site(self) -> Self:
        self.to_domain()
        return self

    def to_domain(self) -> DomainCdnSite:
        return DomainCdnSite.model_validate(self.model_dump())


class SitePatch(Model):
    origin_host: str | None = None
    ssl_mode: SslMode | None = None
    ssl_automatic_mode: SslAutomaticMode | None = None
    minimum_tls_version: MinimumTlsVersion | None = None
    http3_enabled: bool | None = None
    max_upload_size: MaxUploadSize | None = None
    always_use_https: bool | None = None
    under_attack_mode: bool | None = None
    origin_request_host: str | None = None
    origin_sni: str | None = None
    enabled: bool | None = None
    certificate_mode: CertificateMode | None = None
    certificate_path: str | None = None
    certificate_key_path: str | None = None
    cache_enabled: bool | None = None
    cache_query_string_mode: CacheQueryStringMode | None = None
    cache_valid_success: str | None = None
    cache_valid_not_found: str | None = None
    compression: CompressionMode | None = None
    firewall: SiteFirewall | None = None
    visitor_headers: SiteVisitorHeaders | None = None

    def to_domain(self) -> DomainSitePatch:
        return DomainSitePatch.model_validate(self.model_dump(exclude_unset=True))


__all__ = [
    "CdnSite",
    "CdnSiteCreate",
    "SiteFirewall",
    "SitePatch",
    "SitePolicy",
    "SiteVisitorHeaders",
]
