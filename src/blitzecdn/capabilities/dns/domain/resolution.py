"""What a zone's policy becomes for one hostname, and which rule decided it.

All of "first match wins", as a function over values. The rule's name is
returned too, because "why is this hostname not caching" needs both.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict

from blitzecdn.capabilities.dns.domain.rule import Rule
from blitzecdn.capabilities.dns.domain.zone import Domain

__all__ = ["ResolvedPolicy", "merge_overrides", "resolve_policy"]


class ResolvedPolicy(BaseModel):
    """How one hostname is served, and what decided it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fqdn: str
    #: The merged policy, still a ``Domain``, so every zone invariant is checked
    #: and an impossible rule is refused here rather than at the edge.
    policy: Domain
    #: The rule that applied, or ``None`` when the zone answered by itself.
    rule: str | None = None


def merge_overrides(zone: Domain, overrides: Mapping[str, Any]) -> Domain:
    """The zone a rule's hostnames are served by: its overrides merged, then validated.

    Validated whole, as a ``Domain``: every contract's field and model validators,
    the cross-contract checks on ``SitePolicy``, and the zone's own certificate
    pair. ``Rule`` itself can only check its overrides one setting at a time,
    without the zone they land on, so this is what the editors ask before storing
    a rule or a zone, and what resolution asks of every hostname.

    Raises pydantic's ``ValidationError``, which the API reports as a 422.
    """
    return Domain(name=zone.name, policy=zone.policy.applying(overrides))


def resolve_policy(zone: Domain, rules: Sequence[Rule], fqdn: str) -> ResolvedPolicy:
    """Merge the first matching rule's overrides onto the zone's policy.

    ``rules`` is re-sorted rather than trusted, since order is the semantics.
    """
    for rule in sorted(rules, key=lambda item: item.order):
        if not rule.matches(fqdn):
            continue
        merged = merge_overrides(zone, rule.overrides)
        return ResolvedPolicy(fqdn=fqdn, policy=merged, rule=rule.name)
    return ResolvedPolicy(fqdn=fqdn, policy=zone, rule=None)
