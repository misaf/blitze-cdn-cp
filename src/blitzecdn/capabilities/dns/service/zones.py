"""Zone and record editing, DNS export, and desired-state validation.

Zones, rules, and records are canonical desired state. Virtual hosts are
derived by ``dns.domain.hosts`` and are never stored independently.
See docs/decisions/0001-zone-policy-and-composition.md for the design history."""

from __future__ import annotations

from blitzecdn.capabilities.dns.domain import (
    DnsRecord,
    Domain,
    DomainPatch,
    RecordPatch,
    RecordType,
)
from blitzecdn.capabilities.dns.ports import (
    EventRecorder,
    UnitOfWork,
    ZoneStore,
)
from blitzecdn.core.domain.events import domain_event
from blitzecdn.core.exceptions import NotFoundError


class DnsService:
    """The zone editor."""

    def __init__(
        self,
        *,
        zones: ZoneStore,
        events: EventRecorder,
        uow: UnitOfWork,
    ) -> None:
        self.zones = zones
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
        edge address rather than with the record's own.

        ``value`` is left alone: it is the address the edge fetches from while
        the orange cloud is on, and the DNS answer when it is off. Proxying
        changes what the address means, not what it is.
        """
        return self.update_record(
            domain, name, type_, RecordPatch(proxied=True), operator
        )

    def unproxy(
        self, domain: str, name: str, type_: RecordType, value: str, operator: str
    ) -> DnsRecord:
        """Take a hostname off the edge, answering with ``value`` instead.

        The address is required rather than carried over from the origin. On
        Cloudflare unproxying leaves the origin address behind as the public
        answer, which publishes the origin to anyone who looks; naming the
        replacement is one field more and one surprise fewer.
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

    # -- Reporting -----------------------------------------------------

    def dns_export(self) -> list[dict[str, object]]:
        """Every record, for the system that publishes DNS.

        A proxied record's address stays home: it is the origin the edge fetches
        from, not the public answer, and the published A/AAAA is the fleet's —
        which edge addressing belongs to the DNS system rather than here.
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
        """Report proxy configurations no edge could serve.

        A hostname's proxied records must all point at the same origin: the
        A and the AAAA resolve to the same virtual host, and a virtual host has
        one upstream. Deployment validation also covers state loaded through
        backup restoration or rollback, which can bypass the editing services.
        """
        errors: list[str] = []
        zones = {domain.name for domain in self.zones.list_domains()}
        by_hostname: dict[str, dict[str, list[str]]] = {}
        for record in self.zones.list_records():
            if record.domain not in zones or not record.proxied:
                continue
            by_hostname.setdefault(record.fqdn, {}).setdefault(record.value, []).append(
                record.type.value
            )
        for fqdn, origins in sorted(by_hostname.items()):
            if len(origins) > 1:
                pointed = ", ".join(
                    f"{types[0]} -> {origin}"
                    for origin, types in sorted(origins.items())
                )
                errors.append(
                    f"{fqdn!r} has proxied records pointing at different "
                    f"origins ({pointed}); every proxied record for a hostname "
                    "must point at the same origin, or the others must be "
                    "unproxied"
                )
        return errors
