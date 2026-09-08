"""An override that applies to some hostnames in a zone, and not to others.

A zone carries one policy and most zones want exactly that. A rule is what a
zone says when one hostname does not: ``api.example.com`` caches nothing while
the rest of the zone caches for ten minutes, and saying so should not require a
second object holding a second copy of the other nineteen settings.

So a rule is a *match* and a set of *overrides*, and nothing else. It cannot
hold a whole policy, because a rule that could would be a site again — the same
twenty fields, duplicated per hostname, with nothing making the copies agree.

**First match wins.** The rules in a zone are ordered by ``priority``, and the
first enabled rule whose match covers a hostname is the only one that applies.
Merging every matching rule instead would make the effective policy for a
hostname depend on the intersection of several partial documents, which is a
thing an operator has to simulate in their head before they can predict what a
change does. Cloudflare's page rules settled on the same answer for the same
reason.

The overrides are stored as the fields a rule actually sets, not as a patch
model with unset fields. The two are the same information, and the mapping is
the honest shape for it: what is in it is what the rule changes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from blitzecdn.capabilities.dns.domain.patch import DomainPatch
from blitzecdn.core.domain.validation import SITE_NAME, hostname

__all__ = ["Rule", "RulePatch"]

#: A rule may not change the zone's identity, and ``origin_host`` is not on the
#: list because it may: pointing one hostname at a different origin is the
#: second most common reason to write a rule at all.
_NOT_OVERRIDABLE = frozenset({"name"})


def _validate_match(value: str) -> str:
    """``*``, an exact hostname, or ``*.suffix``. Lowercased, no trailing dot.

    Deliberately not a full expression language. A hostname pattern is what
    every rule anyone has asked for so far matches on, it is the same syntax
    nginx already uses in ``server_name``, and an operator can tell at a glance
    which of two patterns is more specific. An expression language can be added
    later as a second kind of match without any stored rule changing meaning;
    starting with one would have meant designing it before knowing what it is
    for.
    """
    normalized = value.strip().lower().rstrip(".")
    if normalized == "*":
        return normalized
    if normalized.startswith("*."):
        return "*." + hostname(normalized[2:])
    return hostname(normalized)


def _validate_overrides(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Every key a zone setting, every value one the zone would accept.

    Checked by handing the mapping to ``DomainPatch``, which is the model that
    already knows what each setting takes — rather than by listing the fields
    again here, which would be a second answer to drift from the first.

    An empty mapping is refused. A rule that overrides nothing still matches,
    and because the first match wins it would shadow every rule behind it while
    changing nothing: the most confusing thing a rule can do.
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
    return dict(value)


class Rule(BaseModel):
    """One override in a zone: which hostnames it covers, and what it changes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    domain: str
    name: str
    #: Lower runs first, and first match wins. Ties break on the name so that
    #: two rules at the same priority still have one answer rather than
    #: whichever the database returned.
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
        """A rule in one zone cannot reach a hostname in another.

        ``*`` is the exception and means "everything in this zone", which is
        what an operator writing a zone-wide exception expects it to mean.
        """
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
    """A partial update to a rule: every field optional, unset means untouched.

    ``domain`` and ``name`` are absent. Together they are the rule's identity —
    moving a rule to another zone is deleting it and writing another, which is
    also the only way to be sure the new zone's operator meant to have it.

    ``overrides`` is replaced wholesale rather than merged. Merging would leave
    no way to stop overriding a setting: every key would be additive, and a
    rule would accumulate settings it could never shed.
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
