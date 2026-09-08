"""Persistence for DNS zones, the policy on them, and their records.

``_DOMAIN_COLUMNS`` names the zone fields that have columns of their own; the
rest of the model is the ``policy`` document. Keeping the split in one frozen
set rather than in both directions of the mapping is what stops a field being
written to a column and read back out of the JSON, which decodes as a zone
whose policy silently lost a setting.
"""

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlmodel import col

from blitzecdn.capabilities.dns.adapters.tables import DnsRecordRow, DomainRow
from blitzecdn.capabilities.dns.domain import DnsRecord, Domain, RecordType
from blitzecdn.core.exceptions import ConflictError, NotFoundError
from blitzecdn.core.persistence.engine import Database

_DOMAIN_COLUMNS = frozenset({"name", "origin_host"})


class ZoneStore:
    """Zones and their records."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def list_domains(self) -> list[Domain]:
        with self._db.session() as session:
            rows = session.scalars(select(DomainRow).order_by(DomainRow.name)).all()
            return [self._domain(row) for row in rows]

    def get_domain(self, name: str) -> Domain:
        with self._db.session() as session:
            row = session.get(DomainRow, name)
            if row is None:
                raise NotFoundError(f"domain {name!r} does not exist")
            return self._domain(row)

    def create_domain(self, domain: Domain) -> Domain:
        with self._db.session() as session:
            session.add(self._domain_row(domain))
            try:
                session.flush()
            except IntegrityError as exc:
                raise ConflictError(f"domain {domain.name!r} already exists") from exc
        return domain

    def replace_domain(self, domain: Domain) -> Domain:
        """Write the zone's policy. The name is its identity and is not moved."""
        with self._db.session() as session:
            row = session.get(DomainRow, domain.name)
            if row is None:
                raise NotFoundError(f"domain {domain.name!r} does not exist")
            self._apply_domain(row, domain)
        return domain

    def delete_domain(self, name: str) -> None:
        with self._db.session() as session:
            row = session.get(DomainRow, name)
            if row is None:
                raise NotFoundError(f"domain {name!r} does not exist")
            # Deletes the zone's records too, via ON DELETE CASCADE.
            session.delete(row)

    def list_records(self, domain: str | None = None) -> list[DnsRecord]:
        query = select(DnsRecordRow).order_by(
            DnsRecordRow.domain, DnsRecordRow.name, DnsRecordRow.type
        )
        if domain is not None:
            query = query.where(col(DnsRecordRow.domain) == domain)
        with self._db.session() as session:
            return [self._record(row) for row in session.scalars(query).all()]

    def get_record(self, domain: str, name: str, type_: RecordType) -> DnsRecord:
        with self._db.session() as session:
            row = session.get(DnsRecordRow, (domain, name, type_.value))
            if row is None:
                raise NotFoundError(
                    f"{type_.value} record {name!r} in {domain!r} does not exist"
                )
            return self._record(row)

    def create_record(self, record: DnsRecord) -> DnsRecord:
        with self._db.session() as session:
            # The foreign key only fires on flush, and a missing zone and a
            # duplicate record both surface as IntegrityError — but the
            # operator needs to know which, so the zone is checked first
            # rather than parsed out of the driver's message afterwards.
            if session.get(DomainRow, record.domain) is None:
                raise NotFoundError(
                    f"domain {record.domain!r} does not exist; add it first"
                )
            session.add(self._row(record))
            try:
                session.flush()
            except IntegrityError as exc:
                raise ConflictError(
                    f"{record.type.value} record {record.name!r} already exists "
                    f"in {record.domain!r}"
                ) from exc
        return record

    def replace_record(
        self, record: DnsRecord, *, expected: DnsRecord | None = None
    ) -> DnsRecord:
        if expected is not None:
            self._db.require_transaction("replace_record(expected=...)")
        key = (record.domain, record.name, record.type.value)
        with self._db.session() as session:
            row = session.get(DnsRecordRow, key)
            if row is None:
                raise NotFoundError(
                    f"{record.type.value} record {record.name!r} in "
                    f"{record.domain!r} does not exist"
                )
            if expected is not None and self._record(row) != expected:
                raise ConflictError(
                    f"{record.type.value} record {record.name!r} changed "
                    "while it was being edited"
                )
            self._apply(row, record)
        return record

    def delete_record(self, domain: str, name: str, type_: RecordType) -> None:
        with self._db.session() as session:
            row = session.get(DnsRecordRow, (domain, name, type_.value))
            if row is None:
                raise NotFoundError(
                    f"{type_.value} record {name!r} in {domain!r} does not exist"
                )
            session.delete(row)

    def replace_all_records(
        self, domains: list[Domain], records: list[DnsRecord]
    ) -> None:
        """Restore zones wholesale. Used by rollback."""
        with self._db.session() as session:
            session.execute(delete(DnsRecordRow))
            session.execute(delete(DomainRow))
            session.flush()
            session.add_all([self._domain_row(domain) for domain in domains])
            session.flush()
            session.add_all([self._row(record) for record in records])

    def _domain_row(self, domain: Domain) -> DomainRow:
        row = DomainRow(name=domain.name)
        self._apply_domain(row, domain)
        return row

    def _apply_domain(self, row: DomainRow, domain: Domain) -> None:
        row.origin_host = domain.origin_host
        row.policy = domain.model_dump(mode="json", exclude=set(_DOMAIN_COLUMNS))
        row.updated_at = self._db.now()

    @staticmethod
    def _domain(row: DomainRow) -> Domain:
        return Domain.model_validate(
            {**row.policy, "name": row.name, "origin_host": row.origin_host}
        )

    def _row(self, record: DnsRecord) -> DnsRecordRow:
        row = DnsRecordRow(
            domain=record.domain, name=record.name, type=record.type.value
        )
        self._apply(row, record)
        return row

    def _apply(self, row: DnsRecordRow, record: DnsRecord) -> None:
        row.value = record.value
        row.ttl = record.ttl
        row.proxied = record.proxied
        row.updated_at = self._db.now()

    @staticmethod
    def _record(row: DnsRecordRow) -> DnsRecord:
        return DnsRecord.model_validate(
            {
                "domain": row.domain,
                "name": row.name,
                "type": row.type,
                "value": row.value,
                "ttl": row.ttl,
                "proxied": row.proxied,
            }
        )


__all__ = ["ZoneStore"]
