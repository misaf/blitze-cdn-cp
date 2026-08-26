"""Stable HTTP v1 representations and their domain mappings."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from blitzecdn.domain.dns import DnsRecord as DomainDnsRecord
from blitzecdn.domain.dns import Domain as DomainDomain
from blitzecdn.domain.dns import RecordPatch as DomainRecordPatch
from blitzecdn.domain.dns import RecordType as DomainRecordType
from blitzecdn.domain.edges import Edge as DomainEdge
from blitzecdn.domain.edges import EdgePatch as DomainEdgePatch
from blitzecdn.domain.sites import (
    CacheQueryStringMode,
    CertificateMode,
    MinimumTlsVersion,
    SslAutomaticMode,
    SslMode,
)
from blitzecdn.domain.sites import (
    CdnSite as DomainCdnSite,
)


class V1Model(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_mode_override="validation")


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


class DnsRecord(SitePolicyV1):
    domain: str
    name: str
    type: Literal["A", "AAAA"] = "A"
    value: str
    ttl: int = Field(default=300, ge=1, le=604800)
    proxied: bool = False

    @model_validator(mode="after")
    def valid_record(self) -> Self:
        self.to_domain()
        return self

    def to_domain(self) -> DomainDnsRecord:
        return DomainDnsRecord.model_validate(self.model_dump())

    @classmethod
    def from_domain(cls, value: DomainDnsRecord) -> Self:
        return cls.model_validate(value.model_dump(mode="json"))


class RecordPatch(V1Model):
    value: str | None = None
    ttl: int | None = Field(default=None, ge=1, le=604800)
    proxied: bool | None = None
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

    def to_domain(self) -> DomainRecordPatch:
        return DomainRecordPatch.model_validate(self.model_dump(exclude_unset=True))


class CdnSite(SitePolicyV1):
    name: str
    server_names: tuple[str, ...]
    origin_host: str

    @classmethod
    def from_domain(cls, value: DomainCdnSite) -> Self:
        return cls.model_validate(value.model_dump(mode="json"))


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
        return cls.model_validate(value.model_dump(mode="json"))


class EdgePatch(V1Model):
    host: str | None = None
    user: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    private_key_file: str | None = None
    public_addresses: tuple[str, ...] | None = None
    ssh_sources: tuple[str, ...] | None = None

    def to_domain(self) -> DomainEdgePatch:
        return DomainEdgePatch.model_validate(self.model_dump(exclude_unset=True))
