"""Derived virtual hosts and certificate/automatic SSL writeback.

Zones, rules, and records are canonical desired state. Virtual hosts are
derived by ``dns.domain.hosts`` and are never stored independently.
See docs/decisions/0001-zone-policy-and-composition.md for the design history."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from blitzecdn.capabilities.dns.domain import (
    CdnSite,
    Domain,
    derive_hosts,
)
from blitzecdn.capabilities.dns.domain.hosts import host_source
from blitzecdn.capabilities.dns.ports import (
    EventRecorder,
    RuleOverrides,
    UnitOfWork,
    ZoneStore,
)
from blitzecdn.capabilities.tls.policy import (
    CertificateMode,
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
        """Derive all virtual hosts from current zones, rules, and records.

        This implements the ``SiteReader`` contract exposed as ``platform.sites``."""
        return derive_hosts(
            self.zones.list_domains(),
            self.rules.list_rules(),
            self.zones.list_records(),
        )

    def get_site(self, name: str) -> CdnSite:
        """One virtual host by its derived name.

        A linear scan of a derivation rather than a keyed read. The set is one
        entry per zone plus one per rule that claims a hostname, which is the
        same order of magnitude as the zones themselves, and a lookup index
        over a derived value would be the projection this change removed.
        """
        for site in self.list_sites():
            if site.name == name:
                return site
        raise NotFoundError(f"CDN site {name!r} does not exist")

    # -- The one write an issuer owns ----------------------------------

    def activate_managed_certificate(
        self, site: CdnSite, mode: CertificateMode
    ) -> CdnSite:
        """Record a managed certificate against whatever produced this host.

        The zone, or the rule that bent it. Which one is recovered from the
        host's name, because that name is a function of exactly those two
        things. Writing to the zone in both cases would give a rule's hostnames
        a certificate issued for somebody else's names.
        """
        certificate_path, certificate_key_path = managed_certificate_paths(site.name)
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

        The checks happen outside this service, but the decision is re-checked
        against canonical state at write time. An operator opting out or
        choosing an equal/stronger mode while a scan is running therefore wins.
        """
        current = self.get_site(site_name)
        if current.ssl_automatic_mode is SslAutomaticMode.CUSTOM:
            return None
        if target.security_rank <= current.ssl_mode.security_rank:
            return None
        with self.uow.transaction():
            self._apply_to_source(current, {"ssl_mode": target})
            self.events.record(
                domain_event(
                    operator,
                    "ssl.automatic.upgraded",
                    "site",
                    site_name,
                    {"from": current.ssl_mode.value, "to": target.value},
                )
            )
        return self.get_site(site_name)

    def _apply_to_source(self, site: CdnSite, changes: Mapping[str, Any]) -> None:
        """Write settings onto the zone or the rule this host was derived from.

        A rule takes them as *overrides*, not as a merge into the zone: a rule
        that already differs from its zone would otherwise have the issuer's
        certificate silently applied to every other hostname in the zone.
        """
        source = host_source(
            self.zones.list_domains(), self.rules.list_rules(), site.name
        )
        if source is None:
            raise NotFoundError(f"CDN site {site.name!r} does not exist")
        domain, rule_name = source
        if rule_name is None:
            zone = self.zones.get_domain(domain)
            self.zones.replace_domain(
                Domain.model_validate({**zone.model_dump(), **changes})
            )
            return
        rule = self.rules.get_rule(domain, rule_name)
        self.rules.replace_rule(
            rule.model_copy(update={"overrides": {**rule.overrides, **changes}})
        )
