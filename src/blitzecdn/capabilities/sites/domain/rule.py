"""An override that applies to some hostnames in a zone, and not to others.

A rule is a match and the overrides it sets, never a whole policy. Rules are
ordered by ``priority`` and the first enabled match is the only one applied, so
an operator can predict the result without simulating merges.

A rule with a ``path`` is a path rule. Matching is hostname, then path: a
hostname is served by the first hostname rule (one without a path) that
matches it, and a request on that hostname by the first path rule whose
``match`` and ``path`` both cover it, layered on the hostname's policy. A path
rule overrides only the settings a capability declares path-scoped, or
redirects; TLS, certificates and the origin stay per hostname.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from blitzecdn.capabilities.sites.domain.patch import DomainPatch
from blitzecdn.core.domain.paths import PathRedirect, path_matches
from blitzecdn.core.domain.site_policy import SitePolicy
from blitzecdn.core.domain.validation import hostname
from blitzecdn.core.domain.values import Hostname, RuleName, UrlPath

__all__ = ["Rule", "RulePatch", "validate_rule_action"]

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

    Whether the rule may be empty, and which settings its kind may override, is
    the rule's question: see ``Rule.validate_action``.
    """
    unknown = sorted(set(value) - set(DomainPatch.composed().model_fields))
    if unknown:
        raise ValueError(
            "a rule can only override zone settings; these are not: "
            + ", ".join(unknown)
        )
    forbidden = sorted(set(value) & _NOT_OVERRIDABLE)
    if forbidden:
        raise ValueError("a rule cannot override: " + ", ".join(forbidden))
    # Raises for a value the zone would refuse, with pydantic's own message. One
    # setting at a time: validators that need neighbouring settings, and the
    # cross-contract rules, need the zone too, so ``RuleService`` asks them of the
    # merge before storing.
    DomainPatch.model_validate(dict(value))
    # Who may set ``certificate_mode`` is ``RuleService``'s question: this also runs
    # on rehydrated rules, including the issuer's.
    return dict(value)


def validate_rule_action(
    path: str | None,
    overrides: Mapping[str, Any],
    redirect: PathRedirect | None,
) -> None:
    """A hostname rule overrides; a path rule overrides path settings or redirects.

    An empty rule is refused: it would still match, and shadow every rule after
    it while changing nothing.
    """
    if path is None:
        if redirect is not None:
            raise ValueError(
                "only a path rule can redirect; give the rule a path, such as '/old/'"
            )
        if not overrides:
            raise ValueError(
                "a rule must override at least one setting; one that overrides "
                "nothing would still match, and shadow every rule after it"
            )
        return
    if redirect is not None:
        if overrides:
            raise ValueError(
                "a path rule either redirects or overrides settings, not both: "
                "a redirected request never reaches the settings"
            )
        if redirect.uses_suffix and not path.endswith("/"):
            raise ValueError(
                "a redirect target can name $1 only on a prefix path, one "
                "ending in '/'; an exact path has nothing after it"
            )
        return
    if not overrides:
        raise ValueError(
            "a path rule must override at least one setting or redirect; one "
            "that does neither would still match, and shadow every path rule "
            "after it"
        )
    scoped = SitePolicy.path_setting_names()
    hostname_only = sorted(set(overrides) - scoped)
    if hostname_only:
        raise ValueError(
            "a path rule can override only "
            + ", ".join(sorted(scoped))
            + "; these are decided per hostname, so set them on a rule without "
            "a path: " + ", ".join(hostname_only)
        )


class Rule(BaseModel):
    """One override in a zone: which hostnames it covers, and what it changes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    domain: Hostname
    name: RuleName
    #: Lower runs first; ties break on the name for one deterministic answer.
    priority: int = Field(default=100, ge=1, le=1000)
    match: str = "*"
    #: Makes this a path rule: a prefix (``/static/``) or exact (``/robots.txt``)
    #: match on the normalized request path.
    path: UrlPath | None = None
    overrides: Mapping[str, Any] = Field(default_factory=dict)
    #: A path rule's alternative to overrides: answer with a redirect.
    redirect: PathRedirect | None = None
    enabled: bool = True

    @field_validator("match")
    @classmethod
    def validate_match(cls, value: str) -> str:
        return _validate_match(value)

    @field_validator("overrides")
    @classmethod
    def validate_overrides(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return _validate_overrides(value)

    @model_validator(mode="after")
    def validate_action(self) -> Self:
        """What each kind of rule may do; see :func:`validate_rule_action`."""
        validate_rule_action(self.path, self.overrides, self.redirect)
        return self

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

    @property
    def is_path_rule(self) -> bool:
        return self.path is not None

    def matches_path(self, fqdn: str, path: str) -> bool:
        """Whether this path rule covers a request. Hostname rules cover none."""
        return (
            self.path is not None
            and self.matches(fqdn)
            and path_matches(self.path, path)
        )

    def matches(self, fqdn: str) -> bool:
        """Whether ``match`` covers a hostname. Disabled rules cover nothing."""
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

    ``overrides`` replaces wholesale, so a setting can be taken off a rule. An
    explicit ``None`` for ``path`` or ``redirect`` removes it; the rule as a
    whole is validated after the merge.
    """

    model_config = ConfigDict(extra="forbid")

    priority: int | None = Field(default=None, ge=1, le=1000)
    match: str | None = None
    path: UrlPath | None = None
    overrides: Mapping[str, Any] | None = None
    redirect: PathRedirect | None = None
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
