"""Zones, the policy on them, and the records in them.

The zone editor decides how everything is served, and a record decides only
whether the edge serves a hostname at all. Between them that is the whole of
canonical desired state; the virtual hosts an edge converges are derived from
it by :mod:`~blitzecdn.capabilities.dns.domain.hosts` and stored nowhere.

What left this module is most of what used to be in it, and the reason is the
same each time: the derivation of a site from a record, the flattening of a
hostname into an internal site name, the two certificate writes that reached
into a record because the derived site could not hold them, the check that a
record named a site that existed, the check that a hostname's two records named
the same site, and the ``server_names`` projection with its revision stamp.
Every one of those guarded a relationship between a record and a site. There is
no such relationship left to guard.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from blitzecdn.capabilities.dns.domain import (
    CdnSite,
    DnsRecord,
    Domain,
    DomainPatch,
    RecordPatch,
    RecordType,
    derive_hosts,
    resolve_policy,
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


class DnsService:
    """The zone editor."""

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

    # -- Domains -------------------------------------------------------

    def list_domains(self) -> list[Domain]:
        return self.zones.list_domains()

    def get_domain(self, name: str) -> Domain:
        """One zone, for a caller that needs to read before it writes."""
        return self.zones.get_domain(name)

    def create_domain(self, domain: Domain, operator: str) -> Domain:
        with self.uow.transaction():
            created = self.zones.create_domain(domain)
            self.events.record(
                domain_event(operator, "domain.created", "domain", domain.name)
            )
        return created

    def update_domain(self, name: str, patch: DomainPatch, operator: str) -> Domain:
        """Change the policy every hostname in the zone is served by.

        Merged against the stored zone rather than validated alone: the rules
        that read across two settings — HTTP/3 needing edge TLS, a certificate
        mode agreeing with its two paths — cannot be checked on a patch that
        mentions one of them, and rejecting the merged zone is what stops a
        half-applied pair from reaching an edge.

        Nothing is resynced afterwards. This changes how hostnames are served,
        not which ones exist, and ``server_names`` is a projection of records.
        """
        current = self.zones.get_domain(name)
        changes = patch.model_dump(exclude_unset=True)
        updated = Domain.model_validate({**current.model_dump(), **changes})
        with self.uow.transaction():
            saved = self.zones.replace_domain(updated)
            self.events.record(
                domain_event(
                    operator,
                    "domain.updated",
                    "domain",
                    name,
                    {"fields": sorted(changes)},
                )
            )
        return saved

    def delete_domain(self, name: str, operator: str) -> None:
        """Remove a zone, every record in it, and every rule on it.

        Both go by cascade, and nothing needs recomputing afterwards: the
        virtual hosts a zone produced were derived from the rows that just
        went, so they stop existing by the same act.
        """
        with self.uow.transaction():
            self.zones.delete_domain(name)
            self.events.record(domain_event(operator, "domain.deleted", "domain", name))

    # -- Records -------------------------------------------------------

    def list_records(self, domain: str | None = None) -> list[DnsRecord]:
        if domain is not None:
            self.zones.get_domain(domain)
        return self.zones.list_records(domain)

    def get_record(self, domain: str, name: str, type_: RecordType) -> DnsRecord:
        """One record, for a caller that needs to read before it writes."""
        return self.zones.get_record(domain, name, type_)

    def create_record(self, record: DnsRecord, operator: str) -> DnsRecord:
        with self.uow.transaction():
            created = self.zones.create_record(record)
            self.events.record(
                domain_event(
                    operator,
                    "record.created",
                    "record",
                    created.fqdn,
                    {"type": created.type.value, "proxied": created.proxied},
                )
            )
        return created

    def update_record(
        self,
        domain: str,
        name: str,
        type_: RecordType,
        patch: RecordPatch,
        operator: str,
    ) -> DnsRecord:
        current = self.zones.get_record(domain, name, type_)
        changes = patch.model_dump(exclude_unset=True)
        updated = DnsRecord.model_validate({**current.model_dump(), **changes})
        with self.uow.transaction():
            saved = self.zones.replace_record(updated, expected=current)
            self.events.record(
                domain_event(
                    operator,
                    "record.updated",
                    "record",
                    saved.fqdn,
                    {"fields": sorted(changes)},
                )
            )
        return saved

    def proxy(
        self, domain: str, name: str, type_: RecordType, operator: str
    ) -> DnsRecord:
        """Put a hostname on the edge, served by its zone's policy.

        Only half the switch. The edge starts serving the hostname on the next
        deploy, but the record only reaches clients once DNS answers with an
        edge address rather than with whatever it answered with before.

        ``value`` is cleared in the same call because a proxied record cannot
        carry one: the address DNS answers with is the fleet's, not the
        operator's.
        """
        return self.update_record(
            domain, name, type_, RecordPatch(proxied=True, value=None), operator
        )

    def unproxy(
        self, domain: str, name: str, type_: RecordType, value: str, operator: str
    ) -> DnsRecord:
        """Take a hostname off the edge, answering with ``value`` instead.

        The address is required rather than inferred. On Cloudflare unproxying
        leaves the origin address behind as the public answer, which publishes
        the origin to anyone who looks; naming the replacement is one field
        more and one surprise fewer.
        """
        return self.update_record(
            domain, name, type_, RecordPatch(proxied=False, value=value), operator
        )

    def delete_record(
        self, domain: str, name: str, type_: RecordType, operator: str
    ) -> None:
        record = self.zones.get_record(domain, name, type_)
        with self.uow.transaction():
            self.zones.delete_record(domain, name, type_)
            self.events.record(
                domain_event(operator, "record.deleted", "record", record.fqdn)
            )

    def record_for_hostname(self, fqdn: str) -> DnsRecord:
        """A record answering for this hostname.

        Certificate preflight needs one, for its TTL. Any of them will do — a
        dual-stack hostname's two records carry the same TTL in every case
        worth distinguishing.
        """
        for record in self.zones.list_records():
            if record.fqdn == fqdn:
                return record
        raise NotFoundError(
            f"no DNS record answers for {fqdn!r}. A certificate is issued for "
            "a name the edge answers on; add a proxied record first."
        )

    # -- The virtual hosts, derived ------------------------------------

    def list_sites(self) -> list[CdnSite]:
        """Every virtual host the fleet should serve.

        Derived on every call rather than stored. It used to be a table with a
        revision stamp beside it and a command to repair it, because a table
        that restates canonical state can fall behind it. A function cannot,
        which is why the stamp, the repair command and the staleness check in
        ``validation_errors`` all went at once.

        The name is ``list_sites`` because this is what an installed package is
        handed as ``platform.sites``; see ``dns.ports.SiteReader``.
        """
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

    # -- Reporting -----------------------------------------------------

    def dns_export(self) -> list[dict[str, object]]:
        """Every record, for the system that publishes DNS.

        A proxied record deliberately carries no address: it must resolve to an
        edge, and edge addressing belongs to the DNS system rather than here.
        """
        return [
            {
                "fqdn": record.fqdn,
                "domain": record.domain,
                "name": record.name,
                "type": record.type.value,
                "ttl": record.ttl,
                "proxied": record.proxied,
                **({} if record.proxied else {"value": record.value}),
            }
            for record in self.zones.list_records()
        ]

    def validation_errors(self) -> list[str]:
        """Ways canonical state contradicts itself.

        A backstop as well as a gate: records also arrive from a restored
        backup and from a rollback's wholesale rewrite, neither of which goes
        through an editor.

        There is one check left, and the list is short for a reason worth
        recording. The others asked whether a record named a site that existed,
        whether one hostname's records named two different sites, and whether
        the stored ``server_names`` still matched the records. All three
        guarded a stored relationship between a record and a site; none of them
        can be violated now, because the relationship is computed.

        What remains cannot be: a hostname can be put on the edge before
        anybody says where the edge should fetch from, and that reaches an edge
        as a server block with nothing behind it.
        """
        errors: list[str] = []
        zones = {domain.name: domain for domain in self.zones.list_domains()}
        rules = self.rules.list_rules()
        for record in self.zones.list_records():
            zone = zones.get(record.domain)
            if zone is None or not record.proxied:
                continue
            in_zone = [rule for rule in rules if rule.domain == zone.name]
            if resolve_policy(zone, in_zone, record.fqdn).policy.origin_host is None:
                errors.append(
                    f"{record.fqdn!r} is proxied but nothing says where to "
                    f"fetch it from. Set an origin on {zone.name!r}, or on a "
                    "rule that matches this hostname."
                )
        return errors
