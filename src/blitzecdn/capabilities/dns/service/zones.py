"""Zone and record editing, DNS export, and desired-state validation."""

from __future__ import annotations

from pydantic import ValidationError

from blitzecdn.capabilities.dns.domain import (
    PUBLICATION,
    DnsPublication,
    DnsRecord,
    Domain,
    PolicyCommand,
    RecordPatch,
    RecordType,
    merge_overrides,
    reject_issuer_owned_certificate,
    unservable_hosts,
)
from blitzecdn.capabilities.dns.ports import (
    EventRecorder,
    RuleReader,
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
        rules: RuleReader,
        events: EventRecorder,
        uow: UnitOfWork,
    ) -> None:
        self.zones = zones
        #: Read-only rules: validation resolves hostnames as a deployment will.
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

    def update_domain(self, name: str, command: PolicyCommand, operator: str) -> Domain:
        """Change the policy every hostname in the zone is served by.

        Intent is resolved against the stored zone inside the transaction, and the
        merged zone is validated whole, so cross-field rules (HTTP/3 needing TLS, a
        certificate mode and its paths) hold.

        So is every rule in the zone merged onto the result, and an edit that would
        leave one unservable is refused, naming it. ``RuleService`` refuses such a
        rule when it is written; accepting the zone edit instead would make the
        rule's hostnames drop out of ``derive_hosts`` with nothing said until
        ``blitzecdn validate`` ran. The operator changes or removes the rule first.
        """
        with self.uow.transaction():
            # Read inside the boundary and write against what was read, so a concurrent
            # edit is merged with, not overwritten.
            current = self.zones.get_domain(name)
            changes = command.changes(current.policy)
            reject_issuer_owned_certificate(changes)
            updated = Domain(name=current.name, policy=current.policy.applying(changes))
            self._refuse_breaking_a_rule(updated)
            saved = self.zones.replace_domain(updated, expected=current)
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

    def _refuse_breaking_a_rule(self, zone: Domain) -> None:
        """Raise ``ConflictError`` for the first stored rule this zone cannot carry.

        A conflict, not a 422: the edit is valid on its own, and what it collides
        with is a rule already stored.
        """
        for rule in self.rules.list_rules(zone.name):
            try:
                merge_overrides(zone, rule.overrides)
            except ValidationError as exc:
                reasons = "; ".join(error["msg"] for error in exc.errors())
                raise ConflictError(
                    f"this change would make rule {rule.name!r} in {zone.name!r} "
                    f"unservable ({reasons}); change or delete the rule first"
                ) from exc

    def delete_domain(self, name: str, operator: str) -> None:
        """Remove a zone with its records and rules; its hosts go with them."""
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
        changes = patch.model_dump(exclude_unset=True)
        with self.uow.transaction():
            current = self.zones.get_record(domain, name, type_)
            updated = DnsRecord.model_validate({**current.model_dump(), **changes})
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

        The edge serves it from the next deploy; clients reach it once DNS answers
        with an edge address. ``value`` stays as the origin.
        """
        return self.update_record(
            domain, name, type_, RecordPatch(proxied=True), operator
        )

    def unproxy(
        self, domain: str, name: str, type_: RecordType, value: str, operator: str
    ) -> DnsRecord:
        """Take a hostname off the edge, answering with ``value`` instead.

        Required rather than carried over, so the origin is never published by default.
        """
        return self.update_record(
            domain, name, type_, RecordPatch(proxied=False, value=value), operator
        )

    def delete_record(
        self, domain: str, name: str, type_: RecordType, operator: str
    ) -> None:
        with self.uow.transaction():
            record = self.zones.get_record(domain, name, type_)
            self.zones.delete_record(domain, name, type_)
            self.events.record(
                domain_event(operator, "record.deleted", "record", record.fqdn)
            )

    def record_for_hostname(self, fqdn: str) -> DnsRecord:
        """Any record answering for this hostname; preflight wants its TTL."""
        for record in self.zones.list_records():
            if record.fqdn == fqdn:
                return record
        raise NotFoundError(
            f"no DNS record answers for {fqdn!r}. A certificate is issued for "
            "a name the edge answers on; add a proxied record first."
        )

    # -- Reporting -----------------------------------------------------

    def dns_export(self) -> dict[str, object]:
        """Every record, for the system that publishes DNS, with who that is.

        ``publication`` travels with the records. A proxied record's origin is never
        exported; its public answer is the fleet's address.
        """
        return {
            "publication": PUBLICATION.model_dump(mode="json"),
            "records": [
                {
                    "fqdn": record.fqdn,
                    "domain": record.domain,
                    "name": record.name,
                    "type": record.type.value,
                    "ttl": record.ttl,
                    "proxied": record.proxied,
                    # A proxied hostname needs an edge address only the fleet
                    # knows; an unproxied one needs exactly this value.
                    "answer": ("an edge address" if record.proxied else record.value),
                    **({} if record.proxied else {"value": record.value}),
                }
                for record in self.zones.list_records()
            ],
        }

    @staticmethod
    def publication() -> DnsPublication:
        """Whether this installation publishes DNS. It does not."""
        return PUBLICATION

    def validation_errors(self) -> list[str]:
        """Report proxy configurations no edge could serve.

        A hostname's proxied records must share one origin, since a virtual host has
        one upstream. Also catches restored or rolled-back state.
        """
        domains = self.zones.list_domains()
        records = self.zones.list_records()
        # What `derive_hosts` dropped silently, reported here before a deploy.
        errors = [
            refused.message
            for refused in unservable_hosts(domains, self.rules.list_rules(), records)
        ]
        zones = {domain.name for domain in domains}
        by_hostname: dict[str, dict[str, list[str]]] = {}
        for record in records:
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
