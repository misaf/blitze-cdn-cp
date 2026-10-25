"""Derive virtual hosts from zones, rules, and proxied records.

Hostnames group by zone, winning rule, and origin — not by policy equality, so
identical rules keep their identity, and different origins split because a
server block has one upstream. Only groups with proxied records are served. A
group whose policy no longer composes is dropped and reported, so one broken
zone cannot fail every read.

Names are certificate paths: ``example-com`` for a zone, ``example-com--api``
for its ``api`` rule, and an origin slug for each origin after the first.
Renaming a rule changes the name; nothing here moves files.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import ValidationError

from blitzecdn.capabilities.dns.domain.record import DnsRecord
from blitzecdn.capabilities.dns.domain.resolution import resolve_policy
from blitzecdn.capabilities.dns.domain.rule import Rule
from blitzecdn.capabilities.dns.domain.zone import Domain
from blitzecdn.capabilities.serving.domain import CdnSite

__all__ = [
    "UnservableHost",
    "derive_hosts",
    "host_name",
    "host_source",
    "unservable_hosts",
]

#: Two hyphens, because one is legal in both a zone label and a rule name and
#: `example-com-api` would be ambiguous.
_SEPARATOR = "--"

#: Origin slugs are truncated with a digest so base + "--" + slug stays a valid
#: host name and distinct origins stay distinct.
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
        # A name suffix, not a security use; also keeps FIPS builds working.
        digest = hashlib.sha1(origin.encode(), usedforsecurity=False).hexdigest()
        suffix = digest[: _SLUG_DIGEST_BITS // 4]
        slug = f"{slug[:_SLUG_LIMIT]}-{suffix}"
    return slug


def _unique_name(candidate: str, used: set[str]) -> str:
    """A name not yet used by another derived host, with a counter if needed.

    An origin slug can spell a rule name, so collisions are possible; visiting
    groups in order keeps the result deterministic.
    """
    if candidate not in used:
        return candidate
    for counter in range(2, 1000):
        alternative = f"{candidate}-{counter}"
        if alternative not in used:
            return alternative
    raise ValueError(f"cannot derive a unique host name for {candidate!r}")


@dataclass(frozen=True)
class UnservableHost:
    """Hostnames whose policy no longer composes into a virtual host.

    Rollbacks, restores and the certificate issuer write state without the editor's
    guards, so a derivation can meet contradictory policy. Raising would fail every
    fleet read (and surface a stored row as a 422); dropping the host and recording
    why makes the drop visible.
    """

    #: The hostnames that stop being served. Never empty.
    hostnames: tuple[str, ...]
    #: What the model refused, in its own words.
    reason: str
    #: The derived host name, when the failure came late enough to have one. A
    #: rule whose overrides will not merge fails before any group is named.
    name: str | None = None

    @property
    def message(self) -> str:
        """One sentence naming the hostnames, the refusal, and the two fixes."""
        derived = f" (derived host {self.name!r})" if self.name is not None else ""
        return (
            f"{', '.join(self.hostnames)}{derived} cannot be served: "
            f"{self.reason}. The zone or rule these hostnames are served by "
            "holds policy no virtual host can be built from; correct it, or "
            "unproxy the hostnames."
        )


def _reason(exc: ValidationError) -> str:
    """What a model refused, as messages only, without pydantic's framing."""
    return "; ".join(
        str(error["msg"]).removeprefix("Value error, ") for error in exc.errors()
    )


def derive_hosts(
    domains: Sequence[Domain],
    rules: Sequence[Rule],
    records: Sequence[DnsRecord],
) -> list[CdnSite]:
    """Every virtual host the fleet should serve, in a stable order.

    Groups that no longer compose are dropped; :func:`unservable_hosts` reports
    them, and ``DnsService.validation_errors`` shows them to an operator.
    """
    hosts, _ = _derive(domains, rules, records)
    return hosts


def unservable_hosts(
    domains: Sequence[Domain],
    rules: Sequence[Rule],
    records: Sequence[DnsRecord],
) -> list[UnservableHost]:
    """What :func:`derive_hosts` dropped, and why, from the same derivation."""
    _, refused = _derive(domains, rules, records)
    return refused


def _derive(
    domains: Sequence[Domain],
    rules: Sequence[Rule],
    records: Sequence[DnsRecord],
) -> tuple[list[CdnSite], list[UnservableHost]]:
    """The derivation itself: the hosts it could build, and the ones it could not."""
    by_zone = {domain.name: domain for domain in domains}
    rules_by_zone: dict[str, list[Rule]] = {name: [] for name in by_zone}
    for rule in rules:
        if rule.domain in rules_by_zone:
            rules_by_zone[rule.domain].append(rule)

    # Keyed, so a hostname's A and AAAA records are reported once.
    refusals: dict[tuple[str | None, tuple[str, ...]], str] = {}

    hostnames: dict[tuple[str, str | None, str], list[str]] = {}
    for record in records:
        zone = by_zone.get(record.domain)
        if zone is None or not record.proxied:
            continue
        try:
            resolved = resolve_policy(zone, rules_by_zone[zone.name], record.fqdn)
        except ValidationError as exc:
            # The winning rule's merge is invalid before any group exists, so the
            # hostname is what gets reported.
            refusals[(None, (record.fqdn,))] = _reason(exc)
            continue
        # A and AAAA records for one hostname contribute one `server_name`.
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
                # The first origin keeps the plain name; later ones add a slug.
                candidate = (
                    base if index == 0 else f"{base}{_SEPARATOR}{_origin_slug(origin)}"
                )
                # Claimed even when unservable: a name is a certificate
                # directory, and the next host must not take it meanwhile.
                name = _unique_name(candidate, used)
                used.add(name)
                server_names = tuple(dict.fromkeys(names))
                try:
                    policy = _policy_for(zone, rules_by_zone[zone_name], rule_name)
                    site = CdnSite.model_validate(
                        {
                            **policy.model_dump(),
                            "name": name,
                            "server_names": server_names,
                            "origin_host": origin,
                        }
                    )
                except ValidationError as exc:
                    refusals[(name, server_names)] = _reason(exc)
                    continue
                hosts.append(site)
    return hosts, [
        UnservableHost(hostnames=group, reason=reason, name=derived)
        for (derived, group), reason in sorted(
            refusals.items(), key=lambda item: (item[0][1], item[0][0] or "")
        )
    ]


def _policy_for(zone: Domain, rules: Sequence[Rule], rule_name: str | None) -> Domain:
    """The zone's policy with the group's named rule applied, or the zone's own."""
    if rule_name is None:
        return zone
    rule = next(item for item in rules if item.name == rule_name)
    return Domain(name=zone.name, policy=zone.policy.applying(rule.overrides))


def host_source(
    domains: Sequence[Domain], rules: Sequence[Rule], name: str
) -> tuple[str, str | None] | None:
    """The zone and rule a derived host name came from, or ``None``.

    Computed by asking :func:`host_name` rather than splitting the name, which
    would answer confidently for names no zone produces. Where several bases claim
    a name, the longest matching base wins.
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
