"""What a zone's policy becomes for one hostname, and which rule decided it.

All of "first match wins", as a function over values. The rule's name is
returned too, because "why is this hostname not caching" needs both.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from blitzecdn.capabilities.dns.domain.rule import Rule
from blitzecdn.capabilities.dns.domain.zone import Domain

__all__ = ["ResolvedPolicy", "resolve_policy"]


class ResolvedPolicy(BaseModel):
    """How one hostname is served, and what decided it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fqdn: str
    #: The merged policy, still a ``Domain``, so every zone invariant is checked
    #: and an impossible rule is refused here rather than at the edge.
    policy: Domain
    #: The rule that applied, or ``None`` when the zone answered by itself.
    rule: str | None = None


def resolve_policy(zone: Domain, rules: Sequence[Rule], fqdn: str) -> ResolvedPolicy:
    """Merge the first matching rule's overrides onto the zone's policy.

    ``rules`` is re-sorted rather than trusted, since order is the semantics.
    """
    for rule in sorted(rules, key=lambda item: item.order):
        if not rule.matches(fqdn):
            continue
        merged = Domain(name=zone.name, policy=zone.policy.applying(rule.overrides))
        return ResolvedPolicy(fqdn=fqdn, policy=merged, rule=rule.name)
    return ResolvedPolicy(fqdn=fqdn, policy=zone, rule=None)
