"""The HTTP representations this capability publishes, and the bodies it takes.

A zone and a record both validate by round-tripping through `dns.domain`: the
published shapes carry no rule of their own, so the wire form and the domain
form cannot drift apart while sitting in different packages.

The zone's policy fields are written out below rather than imported from
`sites`' published copy of the same twenty settings. A published shape may not
cross a capability boundary — only `domain`, `policy`, `ports` and `reporting`
may — and that rule is right: what one capability publishes is not another's
to re-export, or removing a field from one API silently removes it from two.

So they are duplicated, and `test_the_api_carries_every_zone_field_the_zone_has`
is what makes the duplication safe. The nested models are named for the zone
rather than the site because two components with one name collide in a single
OpenAPI document. Both sets exist only until `sites` is removed.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, model_validator

from blitzecdn.api.models import Model
from blitzecdn.capabilities.cache.policy import CacheQueryStringMode
from blitzecdn.capabilities.compression.policy import CompressionMode
from blitzecdn.capabilities.dns.domain import CdnSite as DomainCdnSite
from blitzecdn.capabilities.dns.domain import DnsRecord as DomainDnsRecord
from blitzecdn.capabilities.dns.domain import Domain as DomainDomain
from blitzecdn.capabilities.dns.domain import DomainPatch as DomainDomainPatch
from blitzecdn.capabilities.dns.domain import RecordPatch as DomainRecordPatch
from blitzecdn.capabilities.dns.domain import RecordType as DomainRecordType
from blitzecdn.capabilities.dns.domain import ResolvedPolicy as DomainResolvedPolicy
from blitzecdn.capabilities.dns.domain import Rule as DomainRule
from blitzecdn.capabilities.dns.domain import RulePatch as DomainRulePatch
from blitzecdn.capabilities.http.policy import MaxUploadSize
from blitzecdn.capabilities.tls.policy import (
    CertificateMode,
    MinimumTlsVersion,
    SslAutomaticMode,
    SslMode,
)


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
    """A delegated zone and the policy every hostname in it is served by."""

    name: str
    origin_host: str | None = None

    @model_validator(mode="after")
    def valid_domain(self) -> Self:
        self.to_domain()
        return self

    def to_domain(self) -> DomainDomain:
        return DomainDomain.model_validate(self.model_dump())

    @classmethod
    def from_domain(cls, value: DomainDomain) -> Self:
        return cls.model_validate(value.model_dump(mode="json"))


class DomainPatch(Model):
    """A partial update to a zone's policy. Unset fields are left alone.

    ``name`` is absent: a zone is identified by its name and the records hang
    off it, so a patch that could set one would be a rename that silently
    orphaned them.
    """

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
    firewall: ZoneFirewall | None = None
    visitor_headers: ZoneVisitorHeaders | None = None

    def to_domain(self) -> DomainDomainPatch:
        return DomainDomainPatch.model_validate(self.model_dump(exclude_unset=True))


class RecordType(StrEnum):
    A = "A"
    AAAA = "AAAA"

    def to_domain(self) -> DomainRecordType:
        return DomainRecordType(self.value)


class DnsRecord(Model):
    """A record: an address of its own, or a hostname the edge answers for."""

    domain: str
    name: str
    type: Literal["A", "AAAA"] = "A"
    value: str | None = None
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
    """Send ``proxied`` and ``value`` together to move a hostname either way.

    Off the edge is ``{"proxied": false, "value": "203.0.113.9"}``; back onto
    it is ``{"proxied": true, "value": null}``. Neither half is a valid record
    on its own, so both go in one request.
    """

    value: str | None = None
    ttl: int | None = Field(default=None, ge=1, le=604800)
    proxied: bool | None = None

    def to_domain(self) -> DomainRecordPatch:
        return DomainRecordPatch.model_validate(self.model_dump(exclude_unset=True))


class CdnSite(ZonePolicy):
    """One virtual host, as the fleet will be asked to serve it.

    Published without the two identity fields a zone has, and with the two a
    host has instead: ``name`` is derived from the zone and the rule that
    produced it, and ``server_names`` is the hostnames that resolved here.
    """

    name: str
    server_names: tuple[str, ...]
    origin_host: str

    @classmethod
    def from_domain(cls, value: DomainCdnSite) -> Self:
        return cls.model_validate(value.model_dump(mode="json"))


class RuleBody(Model):
    """What a rule matches and what it changes, shared by the read and the write.

    ``overrides`` is the published patch body — the same twenty optional fields
    an operator would send to change the zone itself. Publishing it as a typed
    body rather than a free-form object is what lets a generated client say
    that ``cache_valid_success`` takes a string before the request is sent.
    """

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
        """Round-tripped through the domain model, minus the zone it is in.

        The zone arrives on the path, so the one rule that cannot be checked
        here is that the match falls inside it. The service checks that against
        the real zone, which is where it belongs anyway.
        """
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
    """A partial update to a rule. Unset fields are left alone.

    ``overrides`` is replaced wholesale when present rather than merged; see
    the domain model for why a rule cannot accumulate settings it can't shed.
    """

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
    """How one hostname is served once the zone and its rules are both read.

    ``policy`` is the zone as it applies to this hostname — the same shape the
    zone itself is published as, with the winning rule's overrides already in
    it — and ``rule`` names the rule that applied. Both, because "why is this
    hostname not caching" needs the answer and the reason in one response.
    """

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
