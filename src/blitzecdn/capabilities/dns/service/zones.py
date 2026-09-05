"""Zones, records, and the hostnames they route to a site.

The zone editor no longer decides how anything is served. It owns records, and
the one thing a record decides about a site: which hostnames answer for it.
``resync_hostnames`` rewrites that projection after every record change, and it
is the only writer of ``CdnSite.server_names``.

What left this module is worth naming, because it is most of what used to be
here: the derivation of a whole site from a record, the flattening of a
hostname into an internal site name, and the two certificate writes that had to
reach into a record because the derived site could not hold them. Sites are
canonical now; all three went with them.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict

from blitzecdn.capabilities.dns.domain import DnsRecord, Domain, RecordPatch, RecordType
from blitzecdn.capabilities.dns.ports import (
    EventRecorder,
    SiteHostnames,
    UnitOfWork,
    ZoneStore,
)
from blitzecdn.core.domain.events import domain_event
from blitzecdn.core.exceptions import ConflictError, NotFoundError


class DnsService:
    """The zone editor."""

    def __init__(
        self,
        *,
        zones: ZoneStore,
        sites: SiteHostnames,
        events: EventRecorder,
        uow: UnitOfWork,
    ) -> None:
        self.zones = zones
        self.sites = sites
        self.events = events
        self.uow = uow

    # -- Domains -------------------------------------------------------

    def list_domains(self) -> list[Domain]:
        return self.zones.list_domains()

    def create_domain(self, domain: Domain, operator: str) -> Domain:
        with self.uow.transaction():
            created = self.zones.create_domain(domain)
            self.events.record(
                domain_event(operator, "domain.created", "domain", domain.name)
            )
        return created

    def delete_domain(self, name: str, operator: str) -> None:
        """Remove a zone and every record in it.

        The records go by cascade, so the hostnames they routed have to come
        off their sites in the same breath — hence the resync before returning.
        A site left holding a hostname whose record no longer exists would
        converge a server block for a name DNS no longer answers.
        """
        with self.uow.transaction():
            self.zones.delete_domain(name)
            self.resync_hostnames()
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
        self._require_site_exists(record)
        self._reject_split_hostname(record)
        with self.uow.transaction():
            created = self.zones.create_record(record)
            self.resync_hostnames()
            self.events.record(
                domain_event(
                    operator,
                    "record.created",
                    "record",
                    created.fqdn,
                    {"type": created.type.value, "site": created.site},
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
        self._require_site_exists(updated)
        self._reject_split_hostname(updated)
        with self.uow.transaction():
            saved = self.zones.replace_record(updated, expected=current)
            self.resync_hostnames()
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

    def route_to_site(
        self, domain: str, name: str, type_: RecordType, site: str, operator: str
    ) -> DnsRecord:
        """Put a hostname on the edge, served by ``site``.

        Only half the switch. The edge starts serving the hostname on the next
        deploy, but the record only reaches clients once DNS answers with an
        edge address rather than with whatever it answered with before.
        """
        return self.update_record(
            domain, name, type_, RecordPatch(site=site, value=None), operator
        )

    def stop_routing(
        self, domain: str, name: str, type_: RecordType, value: str, operator: str
    ) -> DnsRecord:
        """Take a hostname off the edge, answering with ``value`` instead.

        The address is required rather than inferred. Unproxying used to leave
        the site's origin behind as the public answer, which published the
        origin to anyone who looked; naming the replacement is one field more
        and one surprise fewer.
        """
        return self.update_record(
            domain, name, type_, RecordPatch(site=None, value=value), operator
        )

    def delete_record(
        self, domain: str, name: str, type_: RecordType, operator: str
    ) -> None:
        record = self.zones.get_record(domain, name, type_)
        with self.uow.transaction():
            self.zones.delete_record(domain, name, type_)
            self.resync_hostnames()
            self.events.record(
                domain_event(operator, "record.deleted", "record", record.fqdn)
            )

    def _require_site_exists(self, record: DnsRecord) -> None:
        """Refuse a record routed to a site that is not there.

        The foreign key would refuse it too, as an IntegrityError on flush with
        the driver's wording. An operator who mistyped a site name should read
        the site name back, so it is checked here first.
        """
        if record.site is None:
            return
        try:
            self.sites.get_site(record.site)
        except NotFoundError:
            raise NotFoundError(
                f"CDN site {record.site!r} does not exist; create it before "
                f"routing {record.fqdn!r} to it"
            ) from None

    def _reject_split_hostname(self, record: DnsRecord) -> None:
        """Refuse a hostname whose records disagree about which site serves it.

        One hostname is one virtual host. Its A and its AAAA record may both be
        routed — that is an ordinary dual-stack hostname and they name the same
        site — but they cannot name different ones, because nginx would be
        handed one ``server_name`` claimed by two server blocks and the first
        would win in silence.
        """
        if record.site is None:
            return
        key = (record.domain, record.name, record.type)
        for existing in self.zones.list_records(record.domain):
            if existing.site is None or existing.fqdn != record.fqdn:
                continue
            if (existing.domain, existing.name, existing.type) == key:
                continue
            if existing.site != record.site:
                raise ConflictError(
                    f"{record.fqdn!r} is already served by site "
                    f"{existing.site!r} through its {existing.type.value} "
                    f"record. A hostname is one virtual host, so its records "
                    f"cannot name two sites — repoint that record, or route "
                    f"this one to {existing.site!r} as well."
                )

    # -- The hostname projection ---------------------------------------

    def resync_hostnames(self) -> None:
        """Rewrite every site's ``server_names`` from the records routed to it."""
        records = self.zones.list_records()
        routed = self._hostnames_by_site(records)
        for site in self.sites.list_sites():
            self.sites.set_server_names(site.name, routed.get(site.name, ()))
        self.sites.set_projection_revision(self._records_revision(records))

    def rebuild_hostname_projection(self) -> None:
        """Repair the hostname projection from its canonical records."""
        with self.uow.transaction():
            self.resync_hostnames()

    @staticmethod
    def _hostnames_by_site(
        records: list[DnsRecord],
    ) -> dict[str, tuple[str, ...]]:
        """Which hostnames route to each site, deduplicated and ordered.

        Deduplicated because a dual-stack hostname is two records and one
        ``server_name``; sorted because the desired-state document is compared
        by value and an order that depended on insertion would show up as drift
        that is not there.
        """
        names: defaultdict[str, set[str]] = defaultdict(set)
        for record in records:
            if record.site is not None:
                names[record.site].add(record.fqdn)
        return {site: tuple(sorted(items)) for site, items in names.items()}

    @staticmethod
    def _records_revision(records: list[DnsRecord]) -> str:
        canonical = "\n".join(
            record.model_dump_json()
            for record in sorted(
                records, key=lambda item: (item.domain, item.name, item.type.value)
            )
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def record_for_site(self, site_name: str) -> DnsRecord:
        """A record routing a hostname to this site.

        Certificate preflight needs one, for its TTL. Any of them will do — a
        dual-stack hostname's two records carry the same TTL in every case
        worth distinguishing, and the site is what both of them point at.
        """
        for record in self.zones.list_records():
            if record.site == site_name:
                return record
        raise NotFoundError(
            f"no DNS record routes a hostname to site {site_name!r}. A "
            "certificate is issued for a name the edge answers on; route one "
            "to this site first."
        )

    # -- Reporting -----------------------------------------------------

    def dns_export(self) -> list[dict[str, object]]:
        """Every record, for the system that publishes DNS.

        A routed record deliberately carries no address: it must resolve to an
        edge, and edge addressing belongs to the DNS system rather than here.
        The site it routes to is reported so the two can be reconciled.
        """
        return [
            {
                "fqdn": record.fqdn,
                "domain": record.domain,
                "name": record.name,
                "type": record.type.value,
                "ttl": record.ttl,
                "proxied": record.proxied,
                **({"site": record.site} if record.site else {"value": record.value}),
            }
            for record in self.zones.list_records()
        ]

    def validation_errors(self) -> list[str]:
        """Ways canonical state contradicts itself.

        Every check here is a backstop as well as a gate: records and sites also
        arrive from a restored backup and from a rollback's wholesale rewrite,
        neither of which goes through an editor.

        Only contradictions between zones, records and sites. Whether a *site*
        is coherent on its own terms is the owning capability's question, asked
        through ``blitzecdn_deployment_checks`` — which is where the "ACME
        cannot issue for a reserved name" refusal went when this stopped being
        the module that knew what a certificate was.
        """
        errors: list[str] = []
        records = self.zones.list_records()
        sites = self.sites.list_sites()
        known = {site.name for site in sites}

        errors.extend(
            f"{record.fqdn!r} routes to site {record.site!r}, which does not exist"
            for record in records
            if record.site is not None and record.site not in known
        )

        claimed: dict[str, str] = {}
        for record in records:
            if record.site is None:
                continue
            owner = claimed.setdefault(record.fqdn, record.site)
            if owner != record.site:
                errors.append(
                    f"{record.fqdn!r} is routed to both {owner!r} and "
                    f"{record.site!r}. One hostname is one virtual host."
                )

        routed = self._hostnames_by_site(records)
        stale = self.sites.projection_revision() != self._records_revision(records)
        if stale or any(
            site.server_names != routed.get(site.name, ()) for site in sites
        ):
            errors.append(
                "the site hostname projection is stale; rebuild it before deploying"
            )
        return errors
