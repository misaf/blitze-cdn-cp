"""Derived virtual hosts, and certificate and automatic SSL writeback.

Writes read inside the transaction they write in.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from blitzecdn.capabilities.dns.domain import Domain, Rule, derive_hosts
from blitzecdn.capabilities.dns.domain.hosts import host_source
from blitzecdn.capabilities.dns.ports import (
    EventRecorder,
    RuleOverrides,
    UnitOfWork,
    ZoneStore,
)
from blitzecdn.capabilities.serving.domain import CdnSite
from blitzecdn.capabilities.tls.contract import (
    CertificateMode,
    CertificateVersion,
    SslAutomaticMode,
    SslMode,
    managed_certificate_paths,
)
from blitzecdn.core.domain.events import domain_event
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

    # -- The one write an issuer owns ----------------------------------

    def activate_managed_certificate(
        self, site: CdnSite, mode: CertificateMode, version: CertificateVersion
    ) -> CdnSite:
        """Record a managed certificate on the zone or rule that produced this host."""
        certificate_path, certificate_key_path = managed_certificate_paths(
            site.name, version
        )
        changes = {
            "certificate_mode": mode,
            "certificate_path": certificate_path,
            "certificate_key_path": certificate_key_path,
        }
        with self.uow.transaction():
            self._apply_to_source(site, changes)
        return self.get_site(site.name)

    def apply_automatic_ssl_upgrade(
        self, site_name: str, target: SslMode, operator: str
    ) -> CdnSite | None:
        """Persist an upgrade only while the host remains enrolled in Auto.

        The scan ran long before this, so both guards are re-checked inside the
        ``BEGIN IMMEDIATE`` transaction: an operator who opts out or picks a stronger
        mode meanwhile wins.
        """
        with self.uow.transaction():
            current = self.get_site(site_name)
            if current.policy.tls.ssl_automatic_mode is SslAutomaticMode.CUSTOM:
                return None
            if target.security_rank <= current.policy.tls.ssl_mode.security_rank:
                return None
            self._apply_to_source(current, {"ssl_mode": target})
            self.events.record(
                domain_event(
                    operator,
                    "ssl.automatic.upgraded",
                    "site",
                    site_name,
                    {"from": current.policy.tls.ssl_mode.value, "to": target.value},
                )
            )
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
            self.zones.replace_domain(
                Domain(name=zone.name, policy=zone.policy.applying(changes)),
                expected=zone,
            )
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
