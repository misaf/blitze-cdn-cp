"""Turning zones, rules and records into the virtual hosts an edge serves.

This is where a site comes from now. Nothing authors one: every proxied record
resolves to a policy — its zone's, bent by the first rule that matches it — and
the hostnames that resolve alike share one ``server`` block, because that is
what an nginx ``server_name`` list is for.

Grouping by *which rule won* rather than by comparing merged policies is
deliberate. Two rules can produce identical settings today and diverge with the
next edit, and a grouping that noticed the coincidence would silently split one
server block into two the moment somebody changed a field. The rule is the
identity of the exception; hostnames that share an exception share a block.

The name is derived and has to be stable, because it is the directory a managed
certificate lives in: ``example-com`` for a zone's own policy, and
``example-com--api`` for the hostnames its ``api`` rule claims. Renaming a rule
therefore moves that directory, which is the same as saying a renamed rule is a
new exception — which it is.

Only groups that actually claim a hostname produce a host. A rule nothing
matches is an exception waiting for a record; a zone with nothing proxied is a
zone we answer DNS for and serve nothing of. Neither is a server block, and
emitting one for either would put an empty ``server_name`` in front of nginx,
which reads that as the default server for the listener.

Unproxied records never reach any of this: the edge does not know those
hostnames exist.
"""

from __future__ import annotations

from collections.abc import Sequence

from blitzecdn.capabilities.dns.domain.host import CdnSite
from blitzecdn.capabilities.dns.domain.record import DnsRecord
from blitzecdn.capabilities.dns.domain.resolution import resolve_policy
from blitzecdn.capabilities.dns.domain.rule import Rule
from blitzecdn.capabilities.dns.domain.zone import Domain

__all__ = ["derive_hosts", "host_name", "host_source"]

#: Two hyphens between the zone and the rule, because one is legal inside both
#: a zone label and a rule name: `example-com-api` could be the `api` rule of
#: `example.com` or the whole policy of `example-com-api.net`, and a
#: certificate directory that two different exceptions can claim is a
#: certificate one of them gets by accident.
_SEPARATOR = "--"


def host_name(domain: str, rule: str | None) -> str:
    """The stable identity of one group of hostnames served alike."""
    base = domain.replace(".", "-")
    return base if rule is None else f"{base}{_SEPARATOR}{rule}"


def derive_hosts(
    domains: Sequence[Domain],
    rules: Sequence[Rule],
    records: Sequence[DnsRecord],
) -> list[CdnSite]:
    """Every virtual host the fleet should serve, in a stable order.

    One per group of hostnames that resolve alike: the zone's own policy, and
    each rule that some proxied record matched.

    A group whose policy has no ``origin_host`` is left out rather than
    rendered without one. That is not the check that catches the mistake —
    ``DnsService.validation_errors`` refuses the deploy and names the hostname
    — but a derivation cannot raise on state that is merely incomplete, since
    it also runs against an old snapshot during a rollback.
    """
    by_zone = {domain.name: domain for domain in domains}
    rules_by_zone: dict[str, list[Rule]] = {name: [] for name in by_zone}
    for rule in rules:
        if rule.domain in rules_by_zone:
            rules_by_zone[rule.domain].append(rule)

    hostnames: dict[tuple[str, str | None], list[str]] = {}
    for record in records:
        zone = by_zone.get(record.domain)
        if zone is None or not record.proxied:
            continue
        resolved = resolve_policy(zone, rules_by_zone[zone.name], record.fqdn)
        # `dict.fromkeys` further down keeps this de-duplicated: the A and the
        # AAAA record for one hostname resolve to the same group and must
        # contribute one `server_name`, not two.
        hostnames.setdefault((zone.name, resolved.rule), []).append(record.fqdn)

    hosts: list[CdnSite] = []
    for zone_name, zone in sorted(by_zone.items()):
        ordered = sorted(rules_by_zone[zone_name], key=lambda item: item.order)
        groups: list[str | None] = [None, *(rule.name for rule in ordered)]
        for rule_name in groups:
            names = hostnames.get((zone_name, rule_name))
            if not names:
                continue
            policy = _policy_for(zone, rules_by_zone[zone_name], rule_name)
            if policy.origin_host is None:
                continue
            hosts.append(
                CdnSite.model_validate(
                    {
                        **policy.model_dump(),
                        "name": host_name(zone_name, rule_name),
                        "server_names": tuple(dict.fromkeys(names)),
                    }
                )
            )
    return hosts


def _policy_for(zone: Domain, rules: Sequence[Rule], rule_name: str | None) -> Domain:
    """The zone's policy with one named rule applied, or none of them.

    Applied by name rather than by re-resolving a hostname: the group already
    knows which rule won, and asking again with one of its hostnames would make
    the answer depend on a member of the group rather than on the group.
    """
    if rule_name is None:
        return zone
    rule = next(item for item in rules if item.name == rule_name)
    return Domain.model_validate({**zone.model_dump(), **rule.overrides})


def host_source(
    domains: Sequence[Domain], rules: Sequence[Rule], name: str
) -> tuple[str, str | None] | None:
    """The zone and rule a derived host name came from, or ``None``.

    The inverse of :func:`host_name`, and computed by asking that function
    rather than by taking the name apart. Splitting on the separator would be a
    second, weaker copy of the naming rule: it would have to know that a zone's
    dots become hyphens, and it would answer confidently for a name no zone
    could ever produce.
    """
    for domain in domains:
        if host_name(domain.name, None) == name:
            return (domain.name, None)
        for rule in rules:
            if rule.domain == domain.name and host_name(domain.name, rule.name) == name:
                return (domain.name, rule.name)
    return None
