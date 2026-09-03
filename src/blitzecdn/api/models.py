"""The control-plane resource representations, and their domain mappings.

These are the shapes the HTTP API publishes for sites, records, zones and
edges. There is one set of them, serving one published version of the API:
see :mod:`blitzecdn.api.operations` for the operational shapes and
:mod:`blitzecdn.api.requests` for the bodies that drive them.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from blitzecdn.capabilities.cache.policy import CacheQueryStringMode
from blitzecdn.capabilities.compression.policy import CompressionMode
from blitzecdn.capabilities.dns.domain import DnsRecord as DomainDnsRecord
from blitzecdn.capabilities.dns.domain import Domain as DomainDomain
from blitzecdn.capabilities.dns.domain import RecordPatch as DomainRecordPatch
from blitzecdn.capabilities.dns.domain import RecordType as DomainRecordType
from blitzecdn.capabilities.edges.domain import Edge as DomainEdge
from blitzecdn.capabilities.edges.domain import EdgePatch as DomainEdgePatch
from blitzecdn.capabilities.sites.domain import CdnSite as DomainCdnSite
from blitzecdn.capabilities.sites.domain import SitePatch as DomainSitePatch
from blitzecdn.capabilities.tls.policy import (
    CertificateMode,
    MinimumTlsVersion,
    SslAutomaticMode,
    SslMode,
)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_mode_override="validation")


class Domain(Model):
    name: str

    @model_validator(mode="after")
    def valid_domain(self) -> Self:
        self.to_domain()
        return self

    def to_domain(self) -> DomainDomain:
        return DomainDomain.model_validate(self.model_dump())


class RecordType(StrEnum):
    A = "A"
    AAAA = "AAAA"

    def to_domain(self) -> DomainRecordType:
        return DomainRecordType(self.value)


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


class DnsRecord(Model):
    """A record: an address of its own, or the site that answers for it."""

    domain: str
    name: str
    type: Literal["A", "AAAA"] = "A"
    value: str | None = None
    ttl: int = Field(default=300, ge=1, le=604800)
    site: str | None = None

    @model_validator(mode="after")
    def valid_record(self) -> Self:
        self.to_domain()
        return self

    def to_domain(self) -> DomainDnsRecord:
        return DomainDnsRecord.model_validate(self.model_dump())

    @classmethod
    def from_domain(cls, value: DomainDnsRecord) -> Self:
        return cls.model_validate(value.model_dump(mode="json"))


class RecordPatch(Model):
    """Send ``site`` as ``null`` together with a ``value`` to unroute a name."""

    value: str | None = None
    ttl: int | None = Field(default=None, ge=1, le=604800)
    site: str | None = None

    def to_domain(self) -> DomainRecordPatch:
        return DomainRecordPatch.model_validate(self.model_dump(exclude_unset=True))


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


class Edge(Model):
    name: str
    host: str
    user: str = "deploy"
    port: int = Field(default=22, ge=1, le=65535)
    private_key_file: str | None = None
    public_addresses: tuple[str, ...] = Field(default=(), max_length=20)
    ssh_sources: tuple[str, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def valid_edge(self) -> Self:
        self.to_domain()
        return self

    def to_domain(self) -> DomainEdge:
        return DomainEdge.model_validate(self.model_dump())

    @classmethod
    def from_domain(cls, value: DomainEdge) -> Self:
        return cls.model_validate(value.model_dump(mode="json"))


class EdgePatch(Model):
    host: str | None = None
    user: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    private_key_file: str | None = None
    public_addresses: tuple[str, ...] | None = None
    ssh_sources: tuple[str, ...] | None = None

    def to_domain(self) -> DomainEdgePatch:
        return DomainEdgePatch.model_validate(self.model_dump(exclude_unset=True))
