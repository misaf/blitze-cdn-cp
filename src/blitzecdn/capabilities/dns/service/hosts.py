"""Derived virtual hosts, and writeback to the zone or rule behind one.

Writes read inside the transaction they write in.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pydantic import ValidationError

from blitzecdn.capabilities.dns.domain import (
    Domain,
    Rule,
    SiteChange,
    derive_hosts,
    merge_overrides,
)
from blitzecdn.capabilities.dns.domain.hosts import host_source
from blitzecdn.capabilities.dns.ports import (
    EventRecorder,
    RuleOverrides,
    UnitOfWork,
    ZoneStore,
)
from blitzecdn.core.domain.site import CdnSite
from blitzecdn.core.exceptions import NotFoundError


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

    # -- The virtual hosts, derived ------------------------------------

    def list_sites(self) -> list[CdnSite]:
        """Derive virtual hosts from one snapshot of zones, rules, and records."""
        with self.uow.transaction():
            domains = self.zones.list_domains()
            rules = self.rules.list_rules()
            records = self.zones.list_records()
        return derive_hosts(domains, rules, records)

    def get_site(self, name: str) -> CdnSite:
        """One virtual host by its derived name, by scanning the derivation."""
        for site in self.list_sites():
            if site.name == name:
                return site
        raise NotFoundError(f"CDN site {name!r} does not exist")

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
        ``DnsService.update_domain``.
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
