"""HTTP models for zones, records, rules, and resolved hosts.

Validation delegates to the domain models; parity tests check the zone API
exposes every domain field.
"""

from __future__ import annotations

from copy import copy
from enum import StrEnum
from typing import Annotated, Any, Literal, Self, cast, get_args

from pydantic import BaseModel, Field, model_validator

from blitzecdn.capabilities.dns.adapters.documents import serving_document
from blitzecdn.capabilities.dns.domain import DnsRecord as DomainDnsRecord
from blitzecdn.capabilities.dns.domain import Domain as DomainDomain
from blitzecdn.capabilities.dns.domain import DomainPatch as DomainDomainPatch
from blitzecdn.capabilities.dns.domain import RecordPatch as DomainRecordPatch
from blitzecdn.capabilities.dns.domain import RecordType as DomainRecordType
from blitzecdn.capabilities.dns.domain import ResolvedPolicy as DomainResolvedPolicy
from blitzecdn.capabilities.dns.domain import Rule as DomainRule
from blitzecdn.capabilities.dns.domain import RulePatch as DomainRulePatch
from blitzecdn.capabilities.dns.domain.patch import setting_annotation
from blitzecdn.core.domain.composed import ComposedModel
from blitzecdn.core.domain.site import CdnSite as DomainCdnSite
from blitzecdn.core.domain.site_policy import SitePolicy
from blitzecdn.entrypoints.api.models import Model
from blitzecdn.entrypoints.api.requests import PatchValue


def _policy_fields() -> dict[str, tuple[Any, Any]]:
    """The zone's settings as the API publishes them: their own defaults."""
    return {
        name: (field.annotation, copy(field))
        for name, field in SitePolicy.setting_fields().items()
    }


def _patch_fields() -> dict[str, tuple[Any, Any]]:
    """Every setting optional. Null clears a nullable one and is refused otherwise."""
    fields: dict[str, tuple[Any, Any]] = {}
    for name, field in SitePolicy.setting_fields().items():
        bare = field.annotation
        block = isinstance(bare, type) and issubclass(bare, BaseModel)
        description = (
            "Omit to keep the current block. A supplied object replaces all of "
            "it, omitted fields taking their defaults, so {} restores every "
            "default. Null is invalid."
            if block
            else field.description
        )
        nullable = type(None) in get_args(bare)
        kind = setting_annotation(field)
        fields[name] = (
            kind if nullable else cast(Any, PatchValue)[kind],
            Field(default=None, description=description),
        )
    return fields


class Domain(Model, ComposedModel):
    """A delegated zone and its serving policy.

    On creation, omitted policy fields use the documented defaults. Records
    inherit this policy unless a matching rule overrides it.
    """

    name: str

    @classmethod
    def composition_source(cls) -> object:
        return SitePolicy.policy_schema()

    @classmethod
    def compose(cls, source: Any) -> type[Self]:
        return cls.compose_fields(_policy_fields())

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


class DomainPatch(Model, ComposedModel):
    """Omitted fields stay unchanged; null is not a request to restore a default.

    Null clears nullable fields such as origin_sni; other fields reject null.
    Firewall and visitor_headers replace their whole block, including defaults
    for omitted nested fields.
    """

    @classmethod
    def composition_source(cls) -> object:
        return SitePolicy.policy_schema()

    @classmethod
    def compose(cls, source: Any) -> type[Self]:
        return cls.compose_fields(_patch_fields())

    def to_domain(self) -> DomainDomainPatch:
        return DomainDomainPatch.model_validate(self.model_dump(exclude_unset=True))


class RecordType(StrEnum):
    A = "A"
    AAAA = "AAAA"

    def to_domain(self) -> DomainRecordType:
        return DomainRecordType(self.value)


class RecordBody(Model):
    """Record fields shared by creation requests and responses."""

    name: str
    type: Literal["A", "AAAA"] = "A"
    value: str
    ttl: int = Field(default=300, ge=1, le=604800)
    proxied: bool = True


class RecordCreate(RecordBody):
    """Create a record in the URL's domain; the body must not repeat the domain."""

    @model_validator(mode="after")
    def valid_record(self) -> Self:
        self.to_domain("example.invalid")
        return self

    def to_domain(self, domain: str) -> DomainDnsRecord:
        return DomainDnsRecord.model_validate({"domain": domain, **self.model_dump()})


class DnsRecord(RecordBody):
    """A record response, including the domain that owns it."""

    domain: str

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

    value: PatchValue[str] = None
    ttl: PatchValue[Annotated[int, Field(ge=1, le=604800)]] = None
    proxied: PatchValue[bool] = None

    def to_domain(self) -> DomainRecordPatch:
        return DomainRecordPatch.model_validate(self.model_dump(exclude_unset=True))


class CdnSite(Model, ComposedModel):
    """One virtual host: a derived ``name`` and the hostnames resolving to it."""

    name: str
    server_names: tuple[str, ...]
    origin_host: str

    @classmethod
    def composition_source(cls) -> object:
        return SitePolicy.policy_schema()

    @classmethod
    def compose(cls, source: Any) -> type[Self]:
        return cls.compose_fields(_policy_fields())

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

    priority: PatchValue[Annotated[int, Field(ge=1, le=1000)]] = None
    match: PatchValue[str] = None
    overrides: PatchValue[DomainPatch] = None
    enabled: PatchValue[bool] = None

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
    "RecordCreate",
    "RecordPatch",
    "RecordType",
    "ResolvedPolicy",
    "Rule",
    "RuleCreate",
    "RulePatch",
]
