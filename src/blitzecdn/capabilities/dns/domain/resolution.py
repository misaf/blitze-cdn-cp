"""What a zone's policy becomes for one hostname, and which rule decided it.

The whole of "first match wins" lives here, in one function over values, so
that the answer can be checked without a database and stated without one: hand
it a zone, the rules of that zone and a hostname, and it says how that hostname
is served.

The result carries the name of the rule that applied, or ``None`` for a
hostname the zone's own policy answers for. That is not decoration. "Why is
this hostname not caching" is the question an operator actually has, and a
resolver that returns only the merged settings makes them re-derive the match
by eye against a list they cannot see the order of.
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
    #: The zone's policy with the winning rule's overrides applied. Still a
    #: ``Domain``: the merged document has to satisfy every rule a zone does —
    #: HTTP/3 needing edge TLS, a certificate mode agreeing with its paths —
    #: and validating it as one is what says so. A rule that produces an
    #: impossible combination is refused here rather than at the edge.
    policy: Domain
    #: The rule that applied, or ``None`` when the zone answered by itself.
    rule: str | None = None


def resolve_policy(zone: Domain, rules: Sequence[Rule], fqdn: str) -> ResolvedPolicy:
    """Merge the first matching rule's overrides onto the zone's policy.

    ``rules`` is expected in priority order — the store returns them that way,
    and it is sorted again here rather than trusted, because "first" is the
    entire semantics and a caller that assembled the list itself should not be
    able to change the answer by accident.
    """
    for rule in sorted(rules, key=lambda item: item.order):
        if not rule.matches(fqdn):
            continue
        merged = Domain.model_validate({**zone.model_dump(), **rule.overrides})
        return ResolvedPolicy(fqdn=fqdn, policy=merged, rule=rule.name)
    return ResolvedPolicy(fqdn=fqdn, policy=zone, rule=None)
