"""Version 2 control-plane representations and their domain mappings."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from blitzecdn.features.compression.policy import CompressionMode
from blitzecdn.features.dns.domain import DnsRecord as DomainDnsRecord
from blitzecdn.features.dns.domain import Domain as DomainDomain
from blitzecdn.features.dns.domain import RecordPatch as DomainRecordPatch
from blitzecdn.features.dns.domain import RecordType as DomainRecordType
from blitzecdn.features.edges.domain import Edge as DomainEdge
from blitzecdn.features.edges.domain import EdgePatch as DomainEdgePatch
from blitzecdn.features.sites.domain import CdnSite as DomainCdnSite
from blitzecdn.features.sites.domain import SitePatch as DomainSitePatch
from blitzecdn.features.sites.policy import CacheQueryStringMode
from blitzecdn.features.tls.policy import (
    CertificateMode,
    MinimumTlsVersion,
    SslAutomaticMode,
    SslMode,
)


class V2Model(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_mode_override="validation")


class Domain(V2Model):
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


class SiteFirewall(V2Model):
    allow_sources: tuple[str, ...] = Field(default=(), max_length=200)
    deny_sources: tuple[str, ...] = Field(default=(), max_length=200)
    allowed_countries: tuple[str, ...] = Field(default=(), max_length=250)
    denied_countries: tuple[str, ...] = Field(default=(), max_length=250)
    denied_methods: tuple[str, ...] = Field(default=(), max_length=20)
    denied_paths: tuple[str, ...] = Field(default=(), max_length=100)


class SiteVisitorHeaders(V2Model):
    """The ``BZ-*`` headers the edge writes on the request to the origin.

    New in version 2. Version 1 is frozen and projects it away, so a v1 client
    neither reports nor can set it; the domain defaults apply either way.
    """

    connecting_ip: bool = True
    ip_country: bool = False


class SitePolicyV2(V2Model):
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


#: Version 2 diverges from version 1 here, so these three carry the version in
#: their class name. FastAPI derives a component's published name from the
#: class, and two identically named models across versions stay one component
#: only while they are structurally identical — the first field v2 gains would
#: otherwise make pydantic disambiguate *both* into module-qualified names,
#: renaming v1's published schema as a side effect of a change to v2. Naming
#: these explicitly is what keeps `CdnSite` meaning what it meant in v1.
class DnsRecordV2(V2Model):
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


class RecordPatchV2(V2Model):
    """Send ``site`` as ``null`` together with a ``value`` to unroute a name."""

    value: str | None = None
    ttl: int | None = Field(default=None, ge=1, le=604800)
    site: str | None = None

    def to_domain(self) -> DomainRecordPatch:
        return DomainRecordPatch.model_validate(self.model_dump(exclude_unset=True))


class CdnSiteV2(SitePolicyV2):
    name: str
    server_names: tuple[str, ...]
    origin_host: str

    @classmethod
    def from_domain(cls, value: DomainCdnSite) -> Self:
        return cls.model_validate(value.model_dump(mode="json"))


class CdnSiteCreateV2(SitePolicyV2):
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


class SitePatchV2(V2Model):
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


class Edge(V2Model):
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


class EdgePatch(V2Model):
    host: str | None = None
    user: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    private_key_file: str | None = None
    public_addresses: tuple[str, ...] | None = None
    ssh_sources: tuple[str, ...] | None = None

    def to_domain(self) -> DomainEdgePatch:
        return DomainEdgePatch.model_validate(self.model_dump(exclude_unset=True))
