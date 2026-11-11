"""What a zone's policy becomes for one hostname, and which rule decided it.

All of "first match wins", as a function over values. The rule's name is
returned too, because "why is this hostname not caching" needs both. Matching
is hostname, then path: the first hostname rule decides the hostname's policy,
and with a request path the first path rule covering it is layered on that.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict

from blitzecdn.capabilities.sites.domain.rule import Rule
from blitzecdn.capabilities.sites.domain.zone import Domain
from blitzecdn.core.domain.paths import PathRedirect

__all__ = ["ResolvedPolicy", "merge_overrides", "resolve_policy"]


class ResolvedPolicy(BaseModel):
    """How one hostname is served, and what decided it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fqdn: str
    #: The merged policy, still a ``Domain``, so every zone invariant is checked
    #: and an impossible rule is refused here rather than at the edge.
    policy: Domain
    #: The hostname rule that applied, or ``None`` when the zone answered by itself.
    rule: str | None = None
    #: The request path asked about, when one was.
    path: str | None = None
    #: The path rule that applied to ``path``, if any.
    path_rule: str | None = None
    #: Set when ``path_rule`` redirects; ``policy`` is then the hostname's.
    redirect: PathRedirect | None = None


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


def resolve_policy(
    zone: Domain, rules: Sequence[Rule], fqdn: str, path: str | None = None
) -> ResolvedPolicy:
    """Merge the first matching hostname rule's overrides onto the zone's policy.

    With ``path``, the first path rule covering it is then layered on top. Rules
    are re-sorted rather than trusted, since order is the semantics.
    """
    ordered = sorted(rules, key=lambda item: item.order)
    policy, decided_by = zone, None
    for rule in ordered:
        if rule.is_path_rule or not rule.matches(fqdn):
            continue
        policy, decided_by = merge_overrides(zone, rule.overrides), rule.name
        break
    resolved = ResolvedPolicy(fqdn=fqdn, policy=policy, rule=decided_by, path=path)
    if path is None:
        return resolved
    for rule in ordered:
        if not rule.matches_path(fqdn, path):
            continue
        if rule.redirect is not None:
            return resolved.model_copy(
                update={"path_rule": rule.name, "redirect": rule.redirect}
            )
        return resolved.model_copy(
            update={
                "path_rule": rule.name,
                "policy": merge_overrides(policy, rule.overrides),
            }
        )
    return resolved
