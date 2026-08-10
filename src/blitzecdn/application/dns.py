"""Zones, records, and the sites derived from them.

Records are the source of truth. Sites are derived and rewritten by
:meth:`DnsService.sync_sites` after every change, so nothing should write to
the sites table directly — an edit made there survives only until the next
record change silently reverts it.
"""

from __future__ import annotations

from blitzecdn.domain.models import (
    CdnSite,
    CertificateMode,
    DnsRecord,
    Domain,
    RecordPatch,
    RecordType,
    managed_certificate_paths,
)
from blitzecdn.exceptions import ConflictError, NotFoundError
from blitzecdn.ports import AuditLog, SiteStore, ZoneStore


class DnsService:
    """The zone editor. Every other service reads sites this one derives."""

    def __init__(self, zones: ZoneStore, sites: SiteStore, audit_log: AuditLog) -> None:
        self.zones = zones
        self.sites = sites
        self.audit_log = audit_log

    # -- Domains -------------------------------------------------------

    def list_domains(self) -> list[Domain]:
        return self.zones.list_domains()

    def create_domain(self, domain: Domain, operator: str) -> Domain:
        created = self.zones.create_domain(domain)
        self.audit_log.audit(operator, "domain.created", "domain", domain.name)
        return created

    def delete_domain(self, name: str, operator: str) -> None:
        """Remove a zone and every record in it.

        The records go by cascade, so their virtual hosts have to come off the
        edge in the same breath — hence the re-derivation before returning.
        """
        self.zones.delete_domain(name)
        self.sync_sites()
        self.audit_log.audit(operator, "domain.deleted", "domain", name)

    # -- Records -------------------------------------------------------

    def list_records(self, domain: str | None = None) -> list[DnsRecord]:
        if domain is not None:
            self.zones.get_domain(domain)
        return self.zones.list_records(domain)

    def get_record(self, domain: str, name: str, type_: RecordType) -> DnsRecord:
        """One record, for a caller that needs to read before it writes."""
        return self.zones.get_record(domain, name, type_)

    def create_record(self, record: DnsRecord, operator: str) -> DnsRecord:
        self._reject_derived_name_collision(record)
        created = self.zones.create_record(record)
        self.sync_sites()
        self.audit_log.audit(
            operator,
            "record.created",
            "record",
            created.fqdn,
            {"type": created.type.value, "proxied": created.proxied},
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
        updated = DnsRecord.model_validate(
            {**current.model_dump(), **patch.model_dump(exclude_unset=True)}
        )
        self._reject_derived_name_collision(updated)
        saved = self.zones.replace_record(updated)
        self.sync_sites()
        self.audit_log.audit(
            operator,
            "record.updated",
            "record",
            saved.fqdn,
            {"fields": sorted(patch.model_fields_set)},
        )
        return saved

    def set_proxied(
        self, domain: str, name: str, type_: RecordType, proxied: bool, operator: str
    ) -> DnsRecord:
        """Turn the CDN on or off for one record.

        Only half the switch. The edge stops or starts serving the hostname on
        the next deploy, but the record only reaches clients once DNS answers
        with the matching address — the edge's for a proxied record, ``value``
        for an unproxied one. Until DNS agrees, an unproxied hostname still
        pointed at an edge gets the catch-all's 444.
        """
        return self.update_record(
            domain, name, type_, RecordPatch(proxied=proxied), operator
        )

    def delete_record(
        self, domain: str, name: str, type_: RecordType, operator: str
    ) -> None:
        record = self.zones.get_record(domain, name, type_)
        self.zones.delete_record(domain, name, type_)
        self.sync_sites()
        self.audit_log.audit(operator, "record.deleted", "record", record.fqdn)

    def _reject_derived_name_collision(self, record: DnsRecord) -> None:
        """Refuse a record whose site name another record already derives.

        Two hostnames can flatten to one internal site name — ``a.b.example.com``
        and ``a-b.example.com`` both give ``a-b-example-com``. Caught here the
        operator gets a clear conflict on the record they just typed; left to
        the derived write it surfaces as a UNIQUE constraint from SQLite.
        """
        if not record.proxied:
            return
        for existing in self.zones.list_records():
            if not existing.proxied or existing.fqdn == record.fqdn:
                continue
            if existing.site_name == record.site_name:
                raise ConflictError(
                    f"{record.fqdn!r} and {existing.fqdn!r} both map to the "
                    f"internal site name {record.site_name!r}. Rename one."
                )

    # -- Derivation ----------------------------------------------------

    def sync_sites(self) -> None:
        """Rewrite the derived sites table from the records that produce it."""
        self.sites.replace_all_sites(self.derive_sites())

    def derive_sites(self) -> list[CdnSite]:
        """Derive the virtual hosts the edge should serve.

        Deduplicated by name rather than trusting the collision check in
        ``_reject_derived_name_collision``: records can also arrive from a
        restored snapshot, and a duplicate there must not turn into a SQLite
        integrity error deep inside a rollback. ``validation_errors()`` still
        reports it.
        """
        sites: dict[str, CdnSite] = {}
        for record in self.zones.list_records():
            site = record.to_site()
            if site is not None:
                sites.setdefault(site.name, site)
        return list(sites.values())

    def record_for_site(self, site_name: str) -> DnsRecord:
        """Find the record a derived site came from.

        Certificate state has to land on the record: writing it to the sites
        table would survive only until the next record change re-derived over
        it.
        """
        for record in self.zones.list_records():
            if record.proxied and record.site_name == site_name:
                return record
        raise NotFoundError(
            f"no proxied DNS record produces site {site_name!r}. Certificates "
            "belong to a proxied record; enable the proxy first."
        )

    def activate_managed_certificate(
        self, site: CdnSite, mode: CertificateMode
    ) -> CdnSite:
        """Point a site's record at the managed certificate paths and re-derive."""
        certificate_path, certificate_key_path = managed_certificate_paths(site.name)
        record = self.record_for_site(site.name)
        self.zones.replace_record(
            DnsRecord.model_validate(
                {
                    **record.model_dump(),
                    "certificate_mode": mode,
                    "certificate_path": certificate_path,
                    "certificate_key_path": certificate_key_path,
                }
            )
        )
        self.sync_sites()
        return self.sites.get_site(site.name)

    # -- Reporting -----------------------------------------------------

    def dns_export(self) -> list[dict[str, object]]:
        """Every record, for the system that publishes DNS.

        A proxied record deliberately carries no address: it must resolve to an
        edge, and edge addressing belongs to the DNS system rather than here.
        ``value`` is still reported as ``origin`` so the two can be reconciled.
        """
        return [
            {
                "fqdn": record.fqdn,
                "domain": record.domain,
                "name": record.name,
                "type": record.type.value,
                "ttl": record.ttl,
                "proxied": record.proxied,
                "origin": record.value,
                **({} if record.proxied else {"value": record.value}),
            }
            for record in self.zones.list_records()
        ]

    def validation_errors(self) -> list[str]:
        """Ways the stored zones and their derived sites contradict each other."""
        errors: list[str] = []

        # Two records can flatten to one site name (a.b.example.com and
        # a-b.example.com both give a-b-example-com), which would silently make
        # one overwrite the other in the derived table.
        derived: dict[str, str] = {}
        for record in self.zones.list_records():
            if not record.proxied:
                continue
            previous = derived.setdefault(record.site_name, record.fqdn)
            if previous != record.fqdn:
                errors.append(
                    f"{record.fqdn!r} and {previous!r} both derive the internal "
                    f"site name {record.site_name!r}. Rename one of them."
                )
            if record.certificate_mode is CertificateMode.REQUESTED and (
                record.domain.endswith((".test", ".invalid", ".localhost", ".example"))
            ):
                errors.append(
                    f"{record.fqdn!r} requests an ACME certificate, but "
                    f"{record.domain!r} is a reserved name (RFC 6761/2606) that "
                    "no public CA will issue for. Upload a certificate instead."
                )

        names: dict[str, str] = {}
        for site in self.sites.list_sites():
            for server_name in site.server_names:
                previous = names.setdefault(server_name, site.name)
                if previous != site.name:
                    errors.append(
                        f"server name {server_name!r} belongs to both "
                        f"{previous!r} and {site.name!r}"
                    )
        return errors
