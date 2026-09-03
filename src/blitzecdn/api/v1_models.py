"""Version 1 control-plane representations and their domain mappings."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


class V1Model(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_mode_override="validation")

    @classmethod
    def _project(cls, value: BaseModel) -> dict[str, Any]:
        """Keep only the fields this version declares.

        Version 1 is frozen, and these models forbid extras, so a policy knob
        added to the domain after v1 shipped would otherwise make every v1 read
        of that resource raise — the version would break precisely because it
        was not changed. Projecting here is what makes "add a field" a v2-only
        edit instead of a v1 outage.

        A v1 client that writes a whole record still gets the domain default
        for anything v1 cannot name; only PATCH, which sends
        ``exclude_unset``, leaves such a field untouched. That is the honest
        reading of a frozen representation: it cannot express what it does not
        have a word for.
        """
        document = value.model_dump(mode="json")
        return {name: document[name] for name in cls.model_fields if name in document}


class Domain(V1Model):
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


class SiteFirewall(V1Model):
    allow_sources: tuple[str, ...] = Field(default=(), max_length=200)
    deny_sources: tuple[str, ...] = Field(default=(), max_length=200)
    allowed_countries: tuple[str, ...] = Field(default=(), max_length=250)
    denied_countries: tuple[str, ...] = Field(default=(), max_length=250)
    denied_methods: tuple[str, ...] = Field(default=(), max_length=20)
    denied_paths: tuple[str, ...] = Field(default=(), max_length=100)


class SitePolicyV1(V1Model):
    ssl_mode: SslMode = SslMode.OFF
    ssl_automatic_mode: SslAutomaticMode = SslAutomaticMode.AUTO
    minimum_tls_version: MinimumTlsVersion = MinimumTlsVersion.TLS_1_2
    always_use_https: bool = False
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
    firewall: SiteFirewall = Field(default_factory=SiteFirewall)


class DnsRecord(V1Model):
    """A record: an address of its own, or the site that answers for it.

    The policy fields this carried through version 1's life are gone, and they
    are gone from the resource rather than projected away, because there is no
    longer anything behind them to report. They belong to ``CdnSite``, which is
    now created and edited in its own right — see the note on
    :class:`CdnSiteCreate`.
    """

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
        return cls.model_validate(cls._project(value))


class RecordPatch(V1Model):
    """Send ``site`` as ``null`` together with a ``value`` to unroute a name."""

    value: str | None = None
    ttl: int | None = Field(default=None, ge=1, le=604800)
    site: str | None = None

    def to_domain(self) -> DomainRecordPatch:
        return DomainRecordPatch.model_validate(self.model_dump(exclude_unset=True))


class CdnSite(SitePolicyV1):
    name: str
    server_names: tuple[str, ...]
    origin_host: str

    @classmethod
    def from_domain(cls, value: DomainCdnSite) -> Self:
        return cls.model_validate(cls._project(value))


class CdnSiteCreate(SitePolicyV1):
    """The body that creates a site.

    ``server_names`` is absent rather than optional. It is not a property of
    the site an operator chooses: it is the set of hostnames whose records
    route here, so it appears on the representation that is read back and on no
    body that writes one.
    """

    name: str
    origin_host: str

    @model_validator(mode="after")
    def valid_site(self) -> Self:
        self.to_domain()
        return self

    def to_domain(self) -> DomainCdnSite:
        return DomainCdnSite.model_validate(self.model_dump())


class SitePatch(V1Model):
    origin_host: str | None = None
    ssl_mode: SslMode | None = None
    ssl_automatic_mode: SslAutomaticMode | None = None
    minimum_tls_version: MinimumTlsVersion | None = None
    always_use_https: bool | None = None
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
    firewall: SiteFirewall | None = None

    def to_domain(self) -> DomainSitePatch:
        return DomainSitePatch.model_validate(self.model_dump(exclude_unset=True))


class Edge(V1Model):
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
        return cls.model_validate(cls._project(value))


class EdgePatch(V1Model):
    host: str | None = None
    user: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    private_key_file: str | None = None
    public_addresses: tuple[str, ...] | None = None
    ssh_sources: tuple[str, ...] | None = None

    def to_domain(self) -> DomainEdgePatch:
        return DomainEdgePatch.model_validate(self.model_dump(exclude_unset=True))
