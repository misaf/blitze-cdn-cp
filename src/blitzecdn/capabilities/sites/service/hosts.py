"""Derived virtual hosts, and writeback to the zone or rule behind one.

Writes read inside the transaction they write in.

The derivation validates every host, which dominates a read once a fleet has
thousands of them, and most reads see the same zones, rules and records as the
last one. The service keeps the last derivation with the snapshot it came from
and reuses it while a fresh snapshot compares equal. Comparing loaded rows is
far cheaper than validating hosts, and keying on the rows themselves rather than
on time or on write hooks means a write made anywhere, by any process, is seen
on the next read.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from blitzecdn.capabilities.sites.domain import (
    DnsRecord,
    Domain,
    Rule,
    SiteChange,
    derive_hosts,
    merge_overrides,
)
from blitzecdn.capabilities.sites.domain.hosts import host_source
from blitzecdn.capabilities.sites.ports import (
    EventRecorder,
    RuleOverrides,
    UnitOfWork,
    ZoneStore,
)
from blitzecdn.core.domain.site import CdnSite
from blitzecdn.core.exceptions import NotFoundError


@dataclass(frozen=True)
class _Derivation:
    """One derivation and the snapshot it was derived from."""

    domains: list[Domain]
    rules: list[Rule]
    records: list[DnsRecord]
    sites: tuple[CdnSite, ...]
    by_name: Mapping[str, CdnSite]

    def derived_from(
        self, domains: list[Domain], rules: list[Rule], records: list[DnsRecord]
    ) -> bool:
        """Whether this derivation was made from exactly this snapshot."""
        return (
            self.domains == domains and self.rules == rules and self.records == records
        )


class HostService:
    """Derived-host reads and writeback to their canonical zone or rule."""

    def __init__(
        self,
        *,
        zones: ZoneStore,
        rules: RuleOverrides,
        events: EventRecorder,
        uow: UnitOfWork,
    ) -> None:
        self.zones = zones
        self.rules = rules
        self.events = events
        self.uow = uow
        # Replaced whole, never mutated, so a concurrent reader sees either the
        # old derivation or the new one.
        self._last: _Derivation | None = None

    # -- The virtual hosts, derived ------------------------------------

    def list_sites(self) -> list[CdnSite]:
        """Derive virtual hosts from one snapshot of zones, rules, and records.

        Deep copies: a host is frozen only at the top, and its policy holds
        dicts a caller could otherwise change under the next reader.
        """
        return [site.model_copy(deep=True) for site in self._derivation().sites]

    def get_site(self, name: str) -> CdnSite:
        """One virtual host by its derived name."""
        site = self._derivation().by_name.get(name)
        if site is None:
            raise NotFoundError(f"CDN site {name!r} does not exist")
        return site.model_copy(deep=True)

    def _derivation(self) -> _Derivation:
        """The derivation of the current snapshot, reused if nothing changed."""
        with self.uow.transaction():
            domains = self.zones.list_domains()
            rules = self.rules.list_rules()
            records = self.zones.list_records()
        last = self._last
        if last is not None and last.derived_from(domains, rules, records):
            return last
        sites = tuple(derive_hosts(domains, rules, records))
        derivation = _Derivation(
            domains=domains,
            rules=rules,
            records=records,
            sites=sites,
            by_name={site.name: site for site in sites},
        )
        self._last = derivation
        return derivation

    # -- Writes another capability owns ---------------------------------

    def change_site(
        self, site_name: str, decide: Callable[[CdnSite], SiteChange | None]
    ) -> CdnSite | None:
        """Write what ``decide`` asks of a host, judged on the host as stored now.

        ``decide`` runs inside the ``BEGIN IMMEDIATE`` transaction that writes,
        so a guard it applies cannot be overtaken by an edit made meanwhile.
        ``None`` from it writes nothing and returns ``None``.
        """
        with self.uow.transaction():
            current = self.get_site(site_name)
            change = decide(current)
            if change is None:
                return None
            self._apply_to_source(current, change.settings)
            if change.event is not None:
                self.events.record(change.event)
        return self.get_site(site_name)

    def _apply_to_source(self, site: CdnSite, changes: Mapping[str, Any]) -> None:
        """Write settings onto the source zone, or as overrides on the source rule."""
        source = host_source(
            self.zones.list_domains(), self.rules.list_rules(), site.name
        )
        if source is None:
            raise NotFoundError(f"CDN site {site.name!r} does not exist")
        domain, rule_name = source
        if rule_name is None:
            zone = self.zones.get_domain(domain)
            updated = Domain(name=zone.name, policy=zone.policy.applying(changes))
            self._keep_rules_servable(zone, updated, changes)
            self.zones.replace_domain(updated, expected=zone)
            return
        rule = self.rules.get_rule(domain, rule_name)
        # `model_validate`, not `model_copy`, so the unattended issuer's write is
        # validated.
        self.rules.replace_rule(
            Rule.model_validate(
                {**rule.model_dump(), "overrides": {**rule.overrides, **changes}}
            ),
            expected=rule,
        )

    def _keep_rules_servable(
        self, zone: Domain, updated: Domain, changes: Mapping[str, Any]
    ) -> None:
        """Pin the zone's old values onto rules the zone write would break.

        A writeback lands on the zone, so every rule inheriting the changed
        settings inherits the new values too. One that overrides something the new
        values cannot sit beside, such as ``certificate_mode="disabled"`` under a
        zone now serving TLS, would drop its hostnames from the fleet. The
        operator never asked for that, so such a rule keeps what it inherited
        before. The operator's own zone edits are refused instead; see
        ``SiteService.update_domain``.
        """
        before = zone.policy.settings()
        for rule in self.rules.list_rules(zone.name):
            # A path rule overrides only path-scoped settings, and is checked
            # against each host it lands on when hosts are derived.
            if rule.is_path_rule:
                continue
            try:
                merge_overrides(updated, rule.overrides)
                continue
            except ValidationError:
                pass
            inherited = {
                key: before[key] for key in changes if key not in rule.overrides
            }
            # A rule that the zone could not carry before is not this write's to fix.
            pinned = {**inherited, **rule.overrides}
            merge_overrides(updated, pinned)
            self.rules.replace_rule(
                Rule.model_validate({**rule.model_dump(), "overrides": pinned}),
                expected=rule,
            )
