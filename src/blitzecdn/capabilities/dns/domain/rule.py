"""An override that applies to some hostnames in a zone, and not to others.

A rule is a match and the overrides it sets, never a whole policy. Rules are
ordered by ``priority`` and the first enabled match is the only one applied, so
an operator can predict the result without simulating merges.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from blitzecdn.capabilities.dns.domain.patch import DomainPatch
from blitzecdn.core.domain.validation import SITE_NAME, hostname

__all__ = ["Rule", "RulePatch"]

#: A rule may not change the zone's identity. The origin is the record's value,
#: not a zone setting.
_NOT_OVERRIDABLE = frozenset({"name"})


def _validate_match(value: str) -> str:
    """``*``, an exact hostname, or ``*.suffix``; lowercased, no trailing dot.

    nginx's ``server_name`` syntax, where specificity is obvious at a glance.
    """
    normalized = value.strip().lower().rstrip(".")
    if normalized == "*":
        return normalized
    if normalized.startswith("*."):
        return "*." + hostname(normalized[2:])
    return hostname(normalized)


def _validate_overrides(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Every key a zone setting and every value acceptable, checked via ``DomainPatch``.

    An empty mapping is refused: it would shadow every later rule while changing
    nothing.
    """
    unknown = sorted(set(value) - set(DomainPatch.model_fields))
    if unknown:
        raise ValueError(
            "a rule can only override zone settings; these are not: "
            + ", ".join(unknown)
        )
    forbidden = sorted(set(value) & _NOT_OVERRIDABLE)
    if forbidden:
        raise ValueError("a rule cannot override: " + ", ".join(forbidden))
    if not value:
        raise ValueError(
            "a rule must override at least one setting; one that overrides "
            "nothing would still match, and shadow every rule after it"
        )
    # Raises for a value the zone would refuse, with pydantic's own message.
    DomainPatch.model_validate(dict(value))
    # Who may set ``certificate_mode`` is ``RuleService``'s question: this also runs
    # on rehydrated rules, including the issuer's.
    return dict(value)


class Rule(BaseModel):
    """One override in a zone: which hostnames it covers, and what it changes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    domain: str
    name: str
    #: Lower runs first; ties break on the name for one deterministic answer.
    priority: int = Field(default=100, ge=1, le=1000)
    match: str = "*"
    overrides: Mapping[str, Any]
    enabled: bool = True

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, value: str) -> str:
        return hostname(value)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not SITE_NAME.fullmatch(normalized):
            raise ValueError(
                "name must start with a letter and contain only a-z, 0-9, and hyphens"
            )
        return normalized

    @field_validator("match")
    @classmethod
    def validate_match(cls, value: str) -> str:
        return _validate_match(value)

    @field_validator("overrides")
    @classmethod
    def validate_overrides(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return _validate_overrides(value)

    @model_validator(mode="after")
    def validate_match_is_inside_the_zone(self) -> Self:
        """A rule cannot reach another zone's hostnames; ``*`` means this whole zone."""
        if self.match == "*":
            return self
        target = self.match.removeprefix("*.")
        if target != self.domain and not target.endswith(f".{self.domain}"):
            raise ValueError(
                f"match {self.match!r} is not inside {self.domain!r}; a rule "
                "applies to hostnames in its own zone"
            )
        return self

    def matches(self, fqdn: str) -> bool:
        """Whether this rule covers a hostname. Disabled rules cover nothing."""
        if not self.enabled:
            return False
        candidate = fqdn.strip().lower().rstrip(".")
        if self.match == "*":
            return candidate == self.domain or candidate.endswith(f".{self.domain}")
        if self.match.startswith("*."):
            return candidate.endswith("." + self.match[2:])
        return candidate == self.match

    @property
    def order(self) -> tuple[int, str]:
        """What "first" means when two rules could both apply."""
        return (self.priority, self.name)


class RulePatch(BaseModel):
    """A partial update to a rule; ``domain`` and ``name``, its identity, are fixed.

    ``overrides`` replaces wholesale, so a setting can be taken off a rule.
    """

    model_config = ConfigDict(extra="forbid")

    priority: int | None = Field(default=None, ge=1, le=1000)
    match: str | None = None
    overrides: Mapping[str, Any] | None = None
    enabled: bool | None = None

    @field_validator("match")
    @classmethod
    def validate_match(cls, value: str | None) -> str | None:
        return None if value is None else _validate_match(value)

    @field_validator("overrides")
    @classmethod
    def validate_overrides(
        cls, value: Mapping[str, Any] | None
    ) -> Mapping[str, Any] | None:
        return None if value is None else _validate_overrides(value)
