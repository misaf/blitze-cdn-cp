"""Stores, one per table group, over the declarative models.

Each store owns its own queries and nothing else's. They share a
:class:`~blitzecdn.infrastructure.engine.Database`, which owns the file, the
schema and the write lock — so a transaction still behaves the way it did when
all of this was one class, while "which code can write to the deployments
table" is answerable by looking at one type.

Every store still speaks in domain models. The ORM rows in
:mod:`blitzecdn.infrastructure.models` never leave this module: a store loads a
row, hands its columns to the pydantic model that owns the invariants, and
returns that. Persistence changed; the contract in :mod:`blitzecdn.ports` did
not.

On optimistic concurrency
-------------------------
``replace_edge`` and ``replace_record`` take an ``expected`` model and must
refuse a write when the stored row has moved underneath the caller. The
previous implementation compared serialised JSON in a ``WHERE`` clause, which
only worked while the whole model lived in one column. They now read the row
and compare domain models inside the transaction instead. That is not weaker:
:meth:`Database.transaction` reserves the SQLite writer with ``BEGIN
IMMEDIATE`` before the read, so no other writer can interleave between the
comparison and the update.

That guarantee is a property of the *caller's* transaction, not of these
methods, so both now refuse an ``expected`` comparison outside one — see
:meth:`Database.require_transaction`. Written down alone, it was an obligation
every future caller had to know about and no test could notice being dropped.
"""

# SQLModel annotates table attributes as instance values while its SQLAlchemy
# foundation exposes the same attributes as expressions on the class. These
# codes occur only in bulk UPDATE/DELETE/upsert expressions SQLModel does not
# wrap; ordinary model construction and store return types remain checked.
# mypy: disable-error-code="attr-defined,arg-type,call-overload,union-attr"

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import CursorResult, Result, delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from blitzecdn.domain.audit import AuditEvent
from blitzecdn.domain.deployments import (
    Deployment,
    DeploymentStatus,
    require_transition,
)
from blitzecdn.domain.dns import DnsRecord, Domain, RecordType
from blitzecdn.domain.edges import Edge
from blitzecdn.domain.events import DomainEvent
from blitzecdn.domain.runs import AnsibleRun, RunStatus
from blitzecdn.domain.sites import CdnSite
from blitzecdn.domain.validation import validate_setting_name
from blitzecdn.exceptions import ConflictError, NotFoundError
from blitzecdn.infrastructure.engine import Database
from blitzecdn.infrastructure.models import (
    AnsibleSettingRow,
    AuditEventRow,
    DeploymentRequirementRow,
    DeploymentRow,
    DnsRecordRow,
    DomainRow,
    EdgeRow,
    ProjectionStateRow,
    SiteRow,
)
from blitzecdn.infrastructure.operation_stores import WorkflowStore

__all__ = [
    "AnsibleSettingStore",
    "AuditLog",
    "Database",
    "DeploymentRequirementStore",
    "DeploymentStore",
    "EdgeStore",
    "SiteStore",
    "WorkflowStore",
    "ZoneStore",
]


class DeploymentRequirementStore:
    def __init__(self, database: Database) -> None:
        self._db = database

    def require(self, kind: str) -> None:
        with self._db.session() as session:
            session.execute(
                sqlite_insert(DeploymentRequirementRow)
                .values(kind=kind, requested_at=self._db.now())
                .on_conflict_do_update(
                    index_elements=[DeploymentRequirementRow.kind],
                    set_={"requested_at": self._db.now()},
                )
            )

    def clear(self, kind: str) -> None:
        with self._db.session() as session:
            session.execute(
                delete(DeploymentRequirementRow).where(
                    DeploymentRequirementRow.kind == kind
                )
            )

    def pending(self, kind: str) -> bool:
        with self._db.session() as session:
            return session.get(DeploymentRequirementRow, kind) is not None


#: Fields of a record that are columns; everything else is CDN policy. Derived
#: from the model rather than typed out twice, so a new policy field lands in
#: `policy` on its own and a new column is a deliberate edit here.
_RECORD_COLUMNS = frozenset({"domain", "name", "type", "value", "ttl", "proxied"})
_SITE_COLUMNS = frozenset({"name", "server_names", "origin_host"})


def _rows_affected(result: Result[Any]) -> int:
    """``rowcount`` for a DML statement.

    ``Session.execute`` is typed as returning ``Result``, which has no
    ``rowcount`` — only the ``CursorResult`` a DML statement actually produces
    does. The narrowing is here rather than at each call site so "how many rows
    did that touch" stays one readable expression.
    """
    return cast("CursorResult[Any]", result).rowcount


class AnsibleSettingStore:
    """Non-secret, fleet-wide Ansible policy stored by the control plane."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def list_settings(self) -> dict[str, Any]:
        with self._db.session() as session:
            rows = session.scalars(
                select(AnsibleSettingRow).order_by(AnsibleSettingRow.name)
            ).all()
            return {row.name: row.value for row in rows}

    def set_setting(self, name: str, value: Any) -> None:
        # Checked here rather than only at the entry point that happens to be
        # the sole writer today. These rows are published to every host at
        # inventory precedence, so the rule protects the fleet, not the CLI.
        name = validate_setting_name(name)
        with self._db.session() as session:
            statement = sqlite_insert(AnsibleSettingRow).values(
                name=name, value=value, updated_at=self._db.now()
            )
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=[AnsibleSettingRow.name],
                    set_={
                        "value": statement.excluded.value,
                        "updated_at": statement.excluded.updated_at,
                    },
                )
            )

    def delete_setting(self, name: str) -> None:
        name = validate_setting_name(name)
        with self._db.session() as session:
            result = _rows_affected(
                session.execute(
                    delete(AnsibleSettingRow).where(AnsibleSettingRow.name == name)
                )
            )
            if result == 0:
                raise NotFoundError(f"Ansible setting {name!r} does not exist")


class EdgeStore:
    """The fleet, and the table the Ansible inventory plugin reads."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def list_edges(self) -> list[Edge]:
        with self._db.session() as session:
            rows = session.scalars(select(EdgeRow).order_by(EdgeRow.name)).all()
            return [self._edge(row) for row in rows]

    def get_edge(self, name: str) -> Edge:
        with self._db.session() as session:
            row = session.get(EdgeRow, name)
            if row is None:
                raise NotFoundError(f"edge {name!r} does not exist")
            return self._edge(row)

    def create_edge(self, edge: Edge) -> Edge:
        with self._db.session() as session:
            session.add(self._row(edge))
            try:
                session.flush()
            except IntegrityError as exc:
                raise ConflictError(f"edge {edge.name!r} already exists") from exc
        return edge

    def replace_edge(self, edge: Edge, *, expected: Edge | None = None) -> Edge:
        if expected is not None:
            self._db.require_transaction("replace_edge(expected=...)")
        with self._db.session() as session:
            row = session.get(EdgeRow, edge.name)
            if row is None:
                raise NotFoundError(f"edge {edge.name!r} does not exist")
            if expected is not None and self._edge(row) != expected:
                raise ConflictError(
                    f"edge {edge.name!r} changed while it was being edited"
                )
            self._apply(row, edge)
        return edge

    def delete_edge(self, name: str) -> None:
        with self._db.session() as session:
            row = session.get(EdgeRow, name)
            if row is None:
                raise NotFoundError(f"edge {name!r} does not exist")
            session.delete(row)

    def _row(self, edge: Edge) -> EdgeRow:
        row = EdgeRow(name=edge.name)
        self._apply(row, edge)
        return row

    def _apply(self, row: EdgeRow, edge: Edge) -> None:
        row.host = edge.host
        row.user = edge.user
        row.port = edge.port
        row.private_key_file = edge.private_key_file
        row.public_addresses = list(edge.public_addresses)
        row.ssh_sources = list(edge.ssh_sources)
        row.updated_at = self._db.now()

    @staticmethod
    def _edge(row: EdgeRow) -> Edge:
        return Edge.model_validate(
            {
                "name": row.name,
                "host": row.host,
                "user": row.user,
                "port": row.port,
                "private_key_file": row.private_key_file,
                "public_addresses": tuple(row.public_addresses),
                "ssh_sources": tuple(row.ssh_sources),
            }
        )


class SiteStore:
    """The derived virtual hosts.

    Nothing should write here except re-derivation from records: an edit made
    directly survives only until the next record change silently reverts it.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    def list_sites(self) -> list[CdnSite]:
        with self._db.session() as session:
            rows = session.scalars(select(SiteRow).order_by(SiteRow.name)).all()
            return [self._site(row) for row in rows]

    def get_site(self, name: str) -> CdnSite:
        with self._db.session() as session:
            row = session.get(SiteRow, name)
            if row is None:
                raise NotFoundError(f"CDN site {name!r} does not exist")
            return self._site(row)

    def create_site(self, site: CdnSite) -> CdnSite:
        with self._db.session() as session:
            session.add(self._row(site))
            try:
                session.flush()
            except IntegrityError as exc:
                raise ConflictError(f"CDN site {site.name!r} already exists") from exc
        return site

    def replace_site(self, site: CdnSite) -> CdnSite:
        with self._db.session() as session:
            row = session.get(SiteRow, site.name)
            if row is None:
                raise NotFoundError(f"CDN site {site.name!r} does not exist")
            self._apply(row, site)
        return site

    def delete_site(self, name: str) -> None:
        with self._db.session() as session:
            row = session.get(SiteRow, name)
            if row is None:
                raise NotFoundError(f"CDN site {name!r} does not exist")
            session.delete(row)

    def replace_all_sites(self, sites: list[CdnSite]) -> None:
        with self._db.session() as session:
            session.execute(delete(SiteRow))
            session.flush()
            session.add_all([self._row(site) for site in sites])

    def projection_revision(self) -> str | None:
        with self._db.session() as session:
            row = session.get(ProjectionStateRow, "sites")
            return row.source_revision if row else None

    def set_projection_revision(self, revision: str) -> None:
        with self._db.session() as session:
            statement = sqlite_insert(ProjectionStateRow).values(
                name="sites", source_revision=revision, projected_at=self._db.now()
            )
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=[ProjectionStateRow.name],
                    set_={
                        "source_revision": statement.excluded.source_revision,
                        "projected_at": statement.excluded.projected_at,
                    },
                )
            )

    def _row(self, site: CdnSite) -> SiteRow:
        row = SiteRow(name=site.name)
        self._apply(row, site)
        return row

    def _apply(self, row: SiteRow, site: CdnSite) -> None:
        row.server_names = list(site.server_names)
        row.origin_host = site.origin_host
        row.policy = site.model_dump(mode="json", exclude=_SITE_COLUMNS)
        row.updated_at = self._db.now()

    @staticmethod
    def _site(row: SiteRow) -> CdnSite:
        return CdnSite.model_validate(
            {
                **row.policy,
                "name": row.name,
                "server_names": tuple(row.server_names),
                "origin_host": row.origin_host,
            }
        )


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


class DeploymentStore:
    """Deployment history.

    ``snapshot_source`` is injected rather than read from the other stores
    directly: a snapshot spans sites and zones, so owning it here would give
    this store a reason to touch every table it otherwise does not.
    """

    def __init__(self, database: Database, snapshot_source: Callable[[], str]) -> None:
        self._db = database
        self._snapshot_source = snapshot_source

    def snapshot(self) -> str:
        """Desired state as it stands right now, not as any deployment saw it.

        Exposed because the callers that want to *record* a snapshot and the
        callers that want to *read the current one* — validate, and choosing a
        rollback target — are the same callers, and making them hold a second
        object for the second question only invited them to be given the whole
        of persistence to get it.
        """
        return self._snapshot_source()

    def create_deployment(
        self,
        operator: str,
        *,
        check_mode: bool,
        rollback_of: str | None = None,
        snapshot: str | None = None,
        host_limit: str | None = None,
        canonical_digest: str | None = None,
    ) -> Deployment:
        deployment_id = uuid4().hex
        with self._db.session() as session:
            row = DeploymentRow(
                id=deployment_id,
                status=DeploymentStatus.QUEUED.value,
                operator=operator,
                check_mode=check_mode,
                rollback_of=rollback_of,
                created_at=self._db.now(),
                snapshot=snapshot or self._snapshot_source(),
                host_limit=host_limit,
                canonical_digest=canonical_digest,
            )
            session.add(row)
            session.flush()
            # Built from the row this call wrote, inside the transaction that
            # wrote it. Re-reading afterwards was a second transaction, so the
            # object returned described whatever the table said by then rather
            # than what was just created.
            return self._deployment(row)

    def transition(
        self,
        deployment_id: str,
        expected: DeploymentStatus,
        target: DeploymentStatus,
        **values: Any,
    ) -> Deployment:
        # The lifecycle table belongs to the domain and is enforced before the
        # write, so an illegal step is refused even when no row would match.
        # The status guard below then owns the race: a lawful step against a
        # stale expected state is a ConflictError, not a ValueError.
        require_transition(expected, target)
        allowed = {"started_at", "finished_at", "result"}
        if set(values) - allowed:
            raise ValueError("unsupported deployment transition fields")
        result = values.get("result")
        if result is not None and not isinstance(result, AnsibleRun):
            raise ValueError("deployment result must be an AnsibleRun")
        with self._db.session() as session:
            row = session.get(DeploymentRow, deployment_id)
            if row is None or row.status != expected.value:
                raise ConflictError(f"deployment {deployment_id} is not {expected}")
            row.status = target.value
            # Each field is left alone when the caller did not supply it —
            # what COALESCE did in the SQL this replaced. A transition that
            # only finishes a run must not blank the time it started.
            for field in ("started_at", "finished_at"):
                supplied = values.get(field)
                if supplied is not None:
                    setattr(row, field, supplied)
            if result is not None:
                row.result = result.model_dump(mode="json")
            # Inside the transaction, for the same reason as above: a caller
            # that just moved a deployment to RUNNING must be handed the
            # deployment it moved, not the state of the row a moment later.
            return self._deployment(row)

    def get_deployment(self, deployment_id: str) -> Deployment:
        with self._db.session() as session:
            row = session.get(DeploymentRow, deployment_id)
            if row is None:
                raise NotFoundError(f"deployment {deployment_id!r} does not exist")
            return self._deployment(row)

    def deployment_snapshot(self, deployment_id: str) -> str:
        with self._db.session() as session:
            row = session.get(DeploymentRow, deployment_id)
            if row is None:
                raise NotFoundError(f"deployment {deployment_id!r} does not exist")
            return row.snapshot

    def list_deployments(self, limit: int = 20) -> list[Deployment]:
        with self._db.session() as session:
            rows = session.scalars(
                select(DeploymentRow)
                .order_by(DeploymentRow.created_at.desc())
                .limit(limit)
            ).all()
            return [self._deployment(row) for row in rows]

    def queued_deployments(self) -> list[Deployment]:
        with self._db.session() as session:
            rows = session.scalars(
                select(DeploymentRow)
                .where(DeploymentRow.status == DeploymentStatus.QUEUED)
                .order_by(DeploymentRow.created_at)
            ).all()
            return [self._deployment(row) for row in rows]

    def abandon_running(self) -> int:
        """Close out deployments the last controller process left in flight.

        They are given a result of their own rather than only a status: every
        reader now expects to find why a deployment ended in `result`, and
        "the controller restarted" is as much an answer as a failed task is.
        """
        now = self._db.now()
        abandoned = AnsibleRun(
            id=uuid4().hex,
            playbook="",
            status=RunStatus.UNSTARTED,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            error="the controller restarted before this deployment completed",
        ).model_dump(mode="json")
        with self._db.session() as session:
            return _rows_affected(
                session.execute(
                    update(DeploymentRow)
                    .where(DeploymentRow.status == DeploymentStatus.RUNNING)
                    .values(
                        status=DeploymentStatus.ABANDONED.value,
                        finished_at=now,
                        result=abandoned,
                    )
                )
            )

    def prune_history(self, keep: int) -> int:
        """Drop the oldest check-mode deployments beyond the newest ``keep``.

        Only check-mode rows. A real deployment is history an operator reads
        after an incident and, more to the point, is what
        :meth:`successful_rollback_target` chooses from — pruning one could
        remove the snapshot the fleet needs to go back to. Check-mode runs can
        never be that: rollback selection filters them out.

        They are also the ones that accumulate. The drift timer fires hourly
        and each firing writes a row carrying a *complete* copy of every zone
        and record, so this table grows by a full desired state every hour
        whether or not anything changed. Run-log retention exists for the same
        reason on the same schedule; this is that policy applied to the rows.
        """
        with self._db.session() as session:
            survivors = (
                select(DeploymentRow.id)
                .where(DeploymentRow.check_mode.is_(True))
                .order_by(DeploymentRow.created_at.desc())
                .limit(keep)
                .scalar_subquery()
            )
            return _rows_affected(
                session.execute(
                    delete(DeploymentRow).where(
                        DeploymentRow.check_mode.is_(True),
                        DeploymentRow.id.not_in(survivors),
                    )
                )
            )

    def successful_rollback_target(self, current_snapshot: str) -> Deployment:
        # `host_limit IS NULL` keeps canaries out of the automatic choice. A
        # limited run only proves one edge reached that snapshot, so rolling
        # the fleet back to it would converge most edges onto a state they
        # were never running. An operator can still name one explicitly.
        with self._db.session() as session:
            row = session.scalars(
                select(DeploymentRow)
                .where(
                    DeploymentRow.status == DeploymentStatus.SUCCEEDED.value,
                    DeploymentRow.check_mode.is_(False),
                    DeploymentRow.snapshot != current_snapshot,
                    DeploymentRow.host_limit.is_(None),
                )
                .order_by(DeploymentRow.created_at.desc())
                .limit(1)
            ).first()
            if row is None:
                raise NotFoundError("no different successful deployment is available")
            return self._deployment(row)

    @staticmethod
    def _deployment(row: DeploymentRow) -> Deployment:
        return Deployment.model_validate(
            {
                "id": row.id,
                "status": row.status,
                "operator": row.operator,
                "check_mode": row.check_mode,
                "host_limit": row.host_limit,
                "rollback_of": row.rollback_of,
                "canonical_digest": row.canonical_digest,
                "created_at": row.created_at,
                "started_at": row.started_at,
                "finished_at": row.finished_at,
                "result": row.result,
            }
        )


class AuditLog:
    """Append-only record of who did what."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def record(self, event: DomainEvent) -> None:
        """Persist the event emitted by a completed application action."""
        self.audit(
            operator=event.operator,
            action=event.action,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            details=event.details,
        )

    def audit(
        self,
        operator: str,
        action: str,
        resource_type: str,
        resource_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        with self._db.session() as session:
            row = AuditEventRow(
                created_at=self._db.now(),
                operator=operator,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                details=details or {},
            )
            session.add(row)
            session.flush()
            return self._audit_event(row)

    def get_audit_event(self, event_id: int) -> AuditEvent:
        with self._db.session() as session:
            row = session.get(AuditEventRow, event_id)
            if row is None:
                raise NotFoundError(f"audit event {event_id} does not exist")
            return self._audit_event(row)

    def list_audit_events(self, limit: int = 100) -> list[AuditEvent]:
        with self._db.session() as session:
            rows = session.scalars(
                select(AuditEventRow).order_by(AuditEventRow.id.desc()).limit(limit)
            ).all()
            return [self._audit_event(row) for row in rows]

    @staticmethod
    def _audit_event(row: AuditEventRow) -> AuditEvent:
        return AuditEvent.model_validate(
            {
                "id": row.id,
                "created_at": row.created_at,
                "operator": row.operator,
                "action": row.action,
                "resource_type": row.resource_type,
                "resource_id": row.resource_id,
                "details": row.details,
            }
        )
