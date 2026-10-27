"""HTTP models for zones, records, rules, and resolved hosts.

Validation delegates to the domain models; parity tests check the zone API
exposes every domain field.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, model_validator

from blitzecdn.capabilities.cache.contract import CacheQueryStringMode
from blitzecdn.capabilities.compression.contract import CompressionMode
from blitzecdn.capabilities.dns.adapters.documents import serving_document
from blitzecdn.capabilities.dns.domain import DnsRecord as DomainDnsRecord
from blitzecdn.capabilities.dns.domain import Domain as DomainDomain
from blitzecdn.capabilities.dns.domain import DomainPatch as DomainDomainPatch
from blitzecdn.capabilities.dns.domain import RecordPatch as DomainRecordPatch
from blitzecdn.capabilities.dns.domain import RecordType as DomainRecordType
from blitzecdn.capabilities.dns.domain import ResolvedPolicy as DomainResolvedPolicy
from blitzecdn.capabilities.dns.domain import Rule as DomainRule
from blitzecdn.capabilities.dns.domain import RulePatch as DomainRulePatch
from blitzecdn.capabilities.http.contract import MaxUploadSize
from blitzecdn.capabilities.serving.contract import SitePolicy
from blitzecdn.capabilities.serving.domain import CdnSite as DomainCdnSite
from blitzecdn.capabilities.tls.contract import (
    CertificateMode,
    MinimumTlsVersion,
    SslAutomaticMode,
    SslMode,
)
from blitzecdn.entrypoints.api.models import Model


class ZoneFirewall(Model):
    allow_sources: tuple[str, ...] = Field(default=(), max_length=200)
    deny_sources: tuple[str, ...] = Field(default=(), max_length=200)
    allowed_countries: tuple[str, ...] = Field(default=(), max_length=250)
    denied_countries: tuple[str, ...] = Field(default=(), max_length=250)
    denied_methods: tuple[str, ...] = Field(default=(), max_length=20)
    denied_paths: tuple[str, ...] = Field(default=(), max_length=100)


class ZoneVisitorHeaders(Model):
    """The ``BZ-*`` headers the edge writes on the request to the origin."""

    connecting_ip: bool = True
    ip_country: bool = False


class ZonePolicy(Model):
    """How every hostname in a zone is served, unless a rule says otherwise."""

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
    firewall: ZoneFirewall = Field(default_factory=ZoneFirewall)
    visitor_headers: ZoneVisitorHeaders = Field(default_factory=ZoneVisitorHeaders)


class Domain(ZonePolicy):
    """A delegated zone and its serving policy.

    On creation, omitted policy fields use the documented defaults. Records
    inherit this policy unless a matching rule overrides it.
    """

    name: str

    @model_validator(mode="after")
    def valid_domain(self) -> Self:
        self.to_domain()
        return self

    def to_domain(self) -> DomainDomain:
        return DomainDomain(
            name=self.name,
            policy=SitePolicy.from_settings(self.model_dump(exclude={"name"})),
        )

    @classmethod
    def from_domain(cls, value: DomainDomain) -> Self:
        return cls.model_validate(serving_document(value))


class DomainPatch(Model):
    """Omitted fields stay unchanged; null is not a request to restore a default.

    Null clears nullable fields such as origin_sni; non-nullable policy fields
    reject null when the merged policy is validated. Firewall and visitor_headers
    replace their whole block, including defaults for omitted nested fields.
    """

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
    firewall: ZoneFirewall | None = Field(
        default=None,
        description=(
            "Omit to preserve all rules. A supplied object replaces every list; "
            "{} clears all rules. Null is invalid."
        ),
    )
    visitor_headers: ZoneVisitorHeaders | None = Field(
        default=None,
        description=(
            "Omit to preserve both switches. A supplied object replaces both; "
            "{} restores their defaults. Null is invalid."
        ),
    )

    def to_domain(self) -> DomainDomainPatch:
        return DomainDomainPatch.model_validate(self.model_dump(exclude_unset=True))


class RecordType(StrEnum):
    A = "A"
    AAAA = "AAAA"

    def to_domain(self) -> DomainRecordType:
        return DomainRecordType(self.value)


class DnsRecord(Model):
    """A record: the address DNS answers with, or the origin the edge serves from."""

    domain: str
    name: str
    type: Literal["A", "AAAA"] = "A"
    value: str
    ttl: int = Field(default=300, ge=1, le=604800)
    proxied: bool = True

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
    """A partial update to a record. ``proxied`` and ``value`` are independent.

    Flipping ``proxied`` never touches the address: off makes it public, on makes
    it the origin. Omitted fields stay unchanged; explicit null is invalid.
    """

    value: str | None = None
    ttl: int | None = Field(default=None, ge=1, le=604800)
    proxied: bool | None = None

    def to_domain(self) -> DomainRecordPatch:
        return DomainRecordPatch.model_validate(self.model_dump(exclude_unset=True))


class CdnSite(ZonePolicy):
    """One virtual host: a derived ``name`` and the hostnames resolving to it."""

    name: str
    server_names: tuple[str, ...]
    origin_host: str

    @classmethod
    def from_domain(cls, value: DomainCdnSite) -> Self:
        return cls.model_validate(serving_document(value))


class RuleBody(Model):
    """What a rule matches and changes; ``overrides`` is the typed zone patch body."""

    priority: int = Field(default=100, ge=1, le=1000)
    match: str = "*"
    overrides: DomainPatch
    enabled: bool = True


class Rule(RuleBody):
    """One override, as it is read back."""

    domain: str
    name: str

    @classmethod
    def from_domain(cls, value: DomainRule) -> Self:
        return cls.model_validate(
            {**value.model_dump(mode="json"), "overrides": dict(value.overrides)}
        )


class RuleCreate(RuleBody):
    """The body that creates a rule. The zone comes from the path."""

    name: str

    @model_validator(mode="after")
    def valid_rule(self) -> Self:
        """Validated by the domain model; the service checks the zone match."""
        self.to_domain("example.invalid", match="*")
        return self

    def to_domain(self, domain: str, *, match: str | None = None) -> DomainRule:
        return DomainRule.model_validate(
            {
                "domain": domain,
                "name": self.name,
                "priority": self.priority,
                "match": self.match if match is None else match,
                "overrides": self.overrides.model_dump(exclude_unset=True),
                "enabled": self.enabled,
            }
        )


class RulePatch(Model):
    """A partial update to a rule; ``overrides``, when present, replaces wholesale."""

    priority: int | None = Field(default=None, ge=1, le=1000)
    match: str | None = None
    overrides: DomainPatch | None = None
    enabled: bool | None = None

    def to_domain(self) -> DomainRulePatch:
        changes = self.model_dump(exclude_unset=True)
        if self.overrides is not None:
            changes["overrides"] = self.overrides.model_dump(exclude_unset=True)
        return DomainRulePatch.model_validate(changes)


class ResolvedPolicy(Model):
    """How one hostname is served, and which rule decided it."""

    fqdn: str
    rule: str | None = None
    policy: Domain

    @classmethod
    def from_domain(cls, value: DomainResolvedPolicy) -> Self:
        return cls(
            fqdn=value.fqdn,
            rule=value.rule,
            policy=Domain.from_domain(value.policy),
        )


__all__ = [
    "CdnSite",
    "DnsRecord",
    "Domain",
    "DomainPatch",
    "RecordPatch",
    "RecordType",
    "ResolvedPolicy",
    "Rule",
    "RuleCreate",
    "RulePatch",
    "ZoneFirewall",
    "ZonePolicy",
    "ZoneVisitorHeaders",
]
