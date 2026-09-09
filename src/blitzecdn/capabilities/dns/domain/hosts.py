"""Derive virtual hosts from zones, rules, and proxied records.

Group hostnames by their zone, winning rule, and origin — not by policy
equality: rules with identical settings remain distinct so later edits preserve
identity, and hostnames proxied to different origins cannot share one server
block, because a block has one upstream. Only groups containing proxied records
produce a server block.

The design this implements is
``docs/decisions/0001-zone-policy-and-composition.md``.

Host names also determine managed certificate paths: ``example-com`` for a
zone and ``example-com--api`` for its ``api`` rule. When a zone and rule group
spans more than one origin, the first keeps the plain name and each further
origin adds a slug — ``example-com`` for the first, ``example-com--203-0-113-9``
for the next. Renaming a rule changes the derived host identity and certificate
path; this function does not move files.
"""

from __future__ import annotations

import hashlib
import re
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

#: A slug from an origin kept short enough that base + "--" + slug still fits a
#: host name; a 253-character origin must not push a certificate directory past
#: what HOST_NAME accepts. Truncation uses a digest so distinct origins stay
#: distinct.
_SLUG_LIMIT = 40

#: Bits of the digest appended when a slug is truncated. Deterministic where
#: ``hash`` would not be — string hashing is salted per process.
_SLUG_DIGEST_BITS = 16


def host_name(domain: str, rule: str | None) -> str:
    """The stable identity of one group of hostnames served alike."""
    base = domain.replace(".", "-")
    return base if rule is None else f"{base}{_SEPARATOR}{rule}"


def _origin_slug(origin: str) -> str:
    """A name-safe slug for one origin, stable for the same string every time."""
    slug = re.sub(r"[^a-z0-9]+", "-", origin.lower()).strip("-")
    if not slug:
        slug = "origin"
    if len(slug) > _SLUG_LIMIT:
        # A stable name suffix, not a secret or a key. `usedforsecurity=False`
        # is that claim stated to the interpreter rather than only to the
        # reader: it leaves the digest byte-identical, keeps this working on a
        # FIPS build where an unqualified sha1 raises, and satisfies both
        # linters at once. A ruff suppression comment spoke to ruff alone, so
        # bandit's B324 went on failing `just audit` with the rationale for it
        # sitting right there in the source.
        digest = hashlib.sha1(origin.encode(), usedforsecurity=False).hexdigest()
        suffix = digest[: _SLUG_DIGEST_BITS // 4]
        slug = f"{slug[:_SLUG_LIMIT]}-{suffix}"
    return slug


def _unique_name(candidate: str, used: set[str]) -> str:
    """A name not yet used by another derived host, appending a counter if needed.

    Collisions are possible although rare: an origin slug can spell a rule name
    (``api.example.com`` and a rule literally named ``api-example-com``), and
    one group's plain name can equal another's slugged one. Resolving collisions
    in the order groups are visited keeps the outcome deterministic.
    """
    if candidate not in used:
        return candidate
    for counter in range(2, 1000):
        alternative = f"{candidate}-{counter}"
        if alternative not in used:
            return alternative
    raise ValueError(f"cannot derive a unique host name for {candidate!r}")


def derive_hosts(
    domains: Sequence[Domain],
    rules: Sequence[Rule],
    records: Sequence[DnsRecord],
) -> list[CdnSite]:
    """Every virtual host the fleet should serve, in a stable order.

    One per group of hostnames that resolve alike: the zone's own policy, and
    each rule that some proxied record matched. A group is identified by its
    zone, its winning rule, and the origin its records proxy to, because a
    server block has a single upstream; hostnames on one policy but different
    origins split into separate sites.

    The origin always exists — a proxied record's ``value`` is where the edge
    fetches from — but a site can also be absent because restoring an old
    snapshot may present policy that no longer agrees (e.g. a certificate pair
    from a zone the rollback does not bring back), which ``CdnSite`` validation
    refuses. That is not the check that catches the mistake —
    ``DnsService.validation_errors`` refuses the deploy and names the hostname
    — but a derivation cannot raise on state that is merely incomplete.
    """
    by_zone = {domain.name: domain for domain in domains}
    rules_by_zone: dict[str, list[Rule]] = {name: [] for name in by_zone}
    for rule in rules:
        if rule.domain in rules_by_zone:
            rules_by_zone[rule.domain].append(rule)

    hostnames: dict[tuple[str, str | None, str], list[str]] = {}
    for record in records:
        zone = by_zone.get(record.domain)
        if zone is None or not record.proxied:
            continue
        resolved = resolve_policy(zone, rules_by_zone[zone.name], record.fqdn)
        # `dict.fromkeys` further down keeps this de-duplicated: the A and the
        # AAAA record for one hostname resolve to the same group — same origin,
        # or validation has already refused — and must contribute one
        # `server_name`, not two.
        hostnames.setdefault((zone.name, resolved.rule, record.value), []).append(
            record.fqdn
        )

    hosts: list[CdnSite] = []
    used: set[str] = set()
    for zone_name, zone in sorted(by_zone.items()):
        ordered = sorted(rules_by_zone[zone_name], key=lambda item: item.order)
        groups: list[str | None] = [None, *(rule.name for rule in ordered)]
        for rule_name in groups:
            base = host_name(zone_name, rule_name)
            origins = sorted(
                {
                    origin
                    for (group_zone, group_rule, origin) in hostnames
                    if group_zone == zone_name and group_rule == rule_name
                }
            )
            for index, origin in enumerate(origins):
                names = hostnames[(zone_name, rule_name, origin)]
                if not names:
                    continue
                policy = _policy_for(zone, rules_by_zone[zone_name], rule_name)
                # The first origin of a group keeps the plain name so that the
                # common one-origin case is exactly as it always was; further
                # origins add a slug, and a name already taken gets a counter.
                candidate = (
                    base if index == 0 else f"{base}{_SEPARATOR}{_origin_slug(origin)}"
                )
                name = _unique_name(candidate, used)
                used.add(name)
                hosts.append(
                    CdnSite.model_validate(
                        {
                            **policy.model_dump(),
                            "name": name,
                            "server_names": tuple(dict.fromkeys(names)),
                            "origin_host": origin,
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

    The inverse of :func:`host_name` (and of the origin-slugged names
    :func:`derive_hosts` adds when a group spans several origins), computed by
    asking that function rather than by taking the name apart. Splitting on the
    separator would be a second, weaker copy of the naming rule: it would have
    to know that a zone's dots become hyphens, and it would answer confidently
    for a name no zone could ever produce.

    Several names can claim one name — ``example-com--api`` is ``api``'s plain
    name and ``example.com``'s ``api``-slugged one — so the longest base that
    exactly equals or prefixes the name wins.
    """
    candidates: list[tuple[str, str, str | None]] = []
    for domain in domains:
        base = host_name(domain.name, None)
        if name == base or name.startswith(f"{base}{_SEPARATOR}"):
            candidates.append((base, domain.name, None))
        for rule in rules:
            if rule.domain != domain.name:
                continue
            base = host_name(domain.name, rule.name)
            if name == base or name.startswith(f"{base}{_SEPARATOR}"):
                candidates.append((base, domain.name, rule.name))
    if not candidates:
        return None
    _, matched_zone, matched_rule = max(
        candidates, key=lambda candidate: len(candidate[0])
    )
    return (matched_zone, matched_rule)
