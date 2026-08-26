"""Persistence for DNS zones and records."""

# mypy: disable-error-code="attr-defined,arg-type,call-overload,union-attr"

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from blitzecdn.domain.dns import DnsRecord, Domain, RecordType
from blitzecdn.exceptions import ConflictError, NotFoundError
from blitzecdn.infrastructure.engine import Database
from blitzecdn.infrastructure.models import DnsRecordRow, DomainRow

_RECORD_COLUMNS = frozenset({"domain", "name", "type", "value", "ttl", "proxied"})


class ZoneStore:
    """Zones and their records — the source of truth sites derive from."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def list_domains(self) -> list[Domain]:
        with self._db.session() as session:
            rows = session.scalars(select(DomainRow).order_by(DomainRow.name)).all()
            return [Domain.model_validate({"name": row.name}) for row in rows]

    def get_domain(self, name: str) -> Domain:
        with self._db.session() as session:
            row = session.get(DomainRow, name)
            if row is None:
                raise NotFoundError(f"domain {name!r} does not exist")
            return Domain.model_validate({"name": row.name})

    def create_domain(self, domain: Domain) -> Domain:
        with self._db.session() as session:
            session.add(DomainRow(name=domain.name, updated_at=self._db.now()))
            try:
                session.flush()
            except IntegrityError as exc:
                raise ConflictError(f"domain {domain.name!r} already exists") from exc
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
            query = query.where(DnsRecordRow.domain == domain)
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
            now = self._db.now()
            session.add_all(
                [DomainRow(name=domain.name, updated_at=now) for domain in domains]
            )
            session.flush()
            session.add_all([self._row(record) for record in records])

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
        row.policy = record.model_dump(mode="json", exclude=_RECORD_COLUMNS)
        row.updated_at = self._db.now()

    @staticmethod
    def _record(row: DnsRecordRow) -> DnsRecord:
        return DnsRecord.model_validate(
            {
                **row.policy,
                "domain": row.domain,
                "name": row.name,
                "type": row.type,
                "value": row.value,
                "ttl": row.ttl,
                "proxied": row.proxied,
            }
        )


__all__ = ["ZoneStore"]
