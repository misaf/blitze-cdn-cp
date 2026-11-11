"""HTTP models for zones, records, rules, and resolved hosts.

Validation delegates to the domain models; parity tests check the zone API
exposes every domain field.
"""

from __future__ import annotations

from copy import copy
from enum import StrEnum
from typing import Annotated, Any, Literal, Self, cast, get_args

from pydantic import BaseModel, Field, model_validator

from blitzecdn.capabilities.sites import serving_document
from blitzecdn.capabilities.sites.domain import DnsRecord as DomainDnsRecord
from blitzecdn.capabilities.sites.domain import Domain as DomainDomain
from blitzecdn.capabilities.sites.domain import DomainPatch as DomainDomainPatch
from blitzecdn.capabilities.sites.domain import RecordPatch as DomainRecordPatch
from blitzecdn.capabilities.sites.domain import RecordType as DomainRecordType
from blitzecdn.capabilities.sites.domain import ResolvedPolicy as DomainResolvedPolicy
from blitzecdn.capabilities.sites.domain import Rule as DomainRule
from blitzecdn.capabilities.sites.domain import RulePatch as DomainRulePatch
from blitzecdn.capabilities.sites.domain.patch import setting_annotation
from blitzecdn.core.domain.composed import ComposedModel
from blitzecdn.core.domain.paths import PathRedirect as DomainPathRedirect
from blitzecdn.core.domain.site import CdnSite as DomainCdnSite
from blitzecdn.core.domain.site_policy import SitePolicy
from blitzecdn.delivery.api.models import Model
from blitzecdn.delivery.api.requests import PatchValue


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
    locations: tuple[PathLocation, ...] = ()

    @classmethod
    def composition_source(cls) -> object:
        return SitePolicy.policy_schema()

    @classmethod
    def compose(cls, source: Any) -> type[Self]:
        return cls.compose_fields(_policy_fields())

    @classmethod
    def from_domain(cls, value: DomainCdnSite) -> Self:
        return cls.model_validate(serving_document(value))


class PathRedirect(Model):
    """Answer the requests a path rule matches with a redirect.

    The host's firewall, rate limit, Under Attack Mode and maintenance mode
    still apply first: a refused client gets its 403 or 429, not the redirect.
    """

    status: Literal[301, 302, 307, 308] = 301
    target: str = Field(
        description=(
            "An absolute http(s) URL or a path on the same host. On a prefix "
            "path it may name $1, what followed the prefix, as the visitor "
            "sent it."
        )
    )
    preserve_query_string: bool = Field(
        default=True, description="Append the visitor's query string."
    )

    @classmethod
    def from_domain(cls, value: DomainPathRedirect) -> Self:
        return cls.model_validate(value.model_dump(mode="json"))


class PathLocation(Model):
    """One path rule as a virtual host serves it."""

    rule: str
    path: str
    server_names: tuple[str, ...]
    redirect: PathRedirect | None = None
    settings: dict[str, Any] = Field(
        default_factory=dict,
        description="The path-scoped settings in effect under this path.",
    )


class RuleBody(Model):
    """What a rule matches and changes; ``overrides`` is the typed zone patch body."""

    priority: int = Field(default=100, ge=1, le=1000)
    match: str = "*"
    path: str | None = Field(
        default=None,
        description=(
            "Makes this a path rule. A path ending in '/' is a prefix "
            "(/static/), any other is exact (/robots.txt), matched against "
            "the decoded, normalized request path."
        ),
    )
    overrides: DomainPatch = Field(default_factory=DomainPatch)
    redirect: PathRedirect | None = Field(
        default=None, description="A path rule's alternative to overrides."
    )
    enabled: bool = True


class Rule(RuleBody):
    """One override, as it is read back."""

    domain: str
    name: str

    @classmethod
    def from_domain(cls, value: DomainRule) -> Self:
        return cls.model_validate(
            {
                **value.model_dump(mode="json", exclude={"overrides"}),
                "overrides": dict(value.overrides),
            }
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
                "path": self.path,
                "overrides": self.overrides.model_dump(exclude_unset=True),
                "redirect": (
                    None if self.redirect is None else self.redirect.model_dump()
                ),
                "enabled": self.enabled,
            }
        )


class RulePatch(Model):
    """A partial update to a rule; ``overrides``, when present, replaces wholesale."""

    priority: PatchValue[Annotated[int, Field(ge=1, le=1000)]] = None
    match: PatchValue[str] = None
    path: str | None = Field(
        default=None, description="Null makes this a hostname rule again."
    )
    overrides: PatchValue[DomainPatch] = None
    redirect: PathRedirect | None = Field(
        default=None, description="Null removes the redirect."
    )
    enabled: PatchValue[bool] = None

    def to_domain(self) -> DomainRulePatch:
        changes = self.model_dump(exclude_unset=True)
        if self.overrides is not None:
            changes["overrides"] = self.overrides.model_dump(exclude_unset=True)
        return DomainRulePatch.model_validate(changes)


class ResolvedPolicy(Model):
    """How one hostname, or one path on it, is served, and which rules decided it."""

    fqdn: str
    rule: str | None = None
    path: str | None = None
    path_rule: str | None = None
    redirect: PathRedirect | None = None
    policy: Domain

    @classmethod
    def from_domain(cls, value: DomainResolvedPolicy) -> Self:
        return cls(
            fqdn=value.fqdn,
            rule=value.rule,
            path=value.path,
            path_rule=value.path_rule,
            redirect=(
                None
                if value.redirect is None
                else PathRedirect.from_domain(value.redirect)
            ),
            policy=Domain.from_domain(value.policy),
        )


__all__ = [
    "CdnSite",
    "DnsRecord",
    "Domain",
    "DomainPatch",
    "PathLocation",
    "PathRedirect",
    "RecordCreate",
    "RecordPatch",
    "RecordType",
    "ResolvedPolicy",
    "Rule",
    "RuleCreate",
    "RulePatch",
]
