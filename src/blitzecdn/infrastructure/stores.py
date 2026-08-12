"""SQLite stores, one per table group.

Each store owns its own SQL and nothing else's. They share a :class:`Database`,
which owns the file, the schema and the write lock — so a transaction still
behaves the way it did when all of this was one class, while "which code can
write to the deployments table" is now answerable by looking at one type.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from blitzecdn.domain.audit import AuditEvent
from blitzecdn.domain.deployments import (
    Deployment,
    DeploymentStatus,
    require_transition,
)
from blitzecdn.domain.dns import DnsRecord, Domain, RecordType
from blitzecdn.domain.edges import EDGE_SCHEMA_VERSION, Edge
from blitzecdn.domain.operations import (
    OutboxEvent,
    Workflow,
    WorkflowKind,
    WorkflowStatus,
    WorkflowStep,
)
from blitzecdn.domain.runs import AnsibleRun, RunStatus
from blitzecdn.domain.sites import CdnSite
from blitzecdn.domain.validation import STORED
from blitzecdn.exceptions import ConflictError, NotFoundError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    name TEXT PRIMARY KEY,
    document TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domains (
    name TEXT PRIMARY KEY,
    document TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- ON DELETE CASCADE is load-bearing rather than convenience: a record
-- outliving its domain would keep deriving a virtual host for a zone we no
-- longer serve. PRAGMA foreign_keys is enabled per connection below.
CREATE TABLE IF NOT EXISTS dns_records (
    domain TEXT NOT NULL
        REFERENCES domains(name) ON DELETE CASCADE,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    document TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (domain, name, type)
);
-- `result` is the JSON of one domain.runs.AnsibleRun: per-host counters, the
-- tasks that changed, the tasks that failed. It replaced the raw stdout and
-- stderr columns, which were both the largest thing in this table and the
-- thing every reader had to re-parse to learn anything. Raw output now lives
-- in a log file the result names, outside the database entirely.
CREATE TABLE IF NOT EXISTS deployments (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    operator TEXT NOT NULL,
    check_mode INTEGER NOT NULL,
    rollback_of TEXT REFERENCES deployments(id),
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    result TEXT,
    snapshot TEXT NOT NULL,
    host_limit TEXT
);
-- The fleet, and the only place it is recorded. Read by two very different
-- things: `EdgeStore` below, and the `blitzecdn` Ansible inventory plugin,
-- which opens this file read-only at the start of every run.
--
-- That second reader is why this table alone carries `schema_version`. Every
-- other table here is read back through the model that wrote it, so a shape
-- change is caught by pydantic in the same process. The plugin lives in
-- `ansible/plugins/inventory/` and may run under a different interpreter with
-- no `blitzecdn` on its path, so it parses `document` as plain JSON and has
-- nothing to validate against — it checks this column first and refuses a
-- version it was not written for, rather than silently publishing a fleet it
-- half understood.
CREATE TABLE IF NOT EXISTS edges (
    name TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    document TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ansible_settings (
    name TEXT PRIMARY KEY,
    document TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    operator TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT,
    details TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    resource_id TEXT,
    status TEXT NOT NULL,
    operator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    steps TEXT NOT NULL DEFAULT '[]',
    error TEXT
);
CREATE INDEX IF NOT EXISTS workflows_status_idx ON workflows(status, updated_at);
CREATE TABLE IF NOT EXISTS outbox_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL,
    event_key TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS outbox_pending_idx
    ON outbox_events(delivered_at, id);
CREATE TABLE IF NOT EXISTS projection_state (
    name TEXT PRIMARY KEY,
    source_revision TEXT NOT NULL,
    projected_at TEXT NOT NULL
);
"""


class Database:
    """The SQLite file: connections, schema, and the process-wide write lock."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self.lock = threading.RLock()
        self._local = threading.local()
        self._initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        ambient = getattr(self._local, "connection", None)
        if ambient is not None:
            yield ambient
            return
        with self._connection() as connection:
            yield connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self._path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Join all nested store calls to one SQLite transaction.

        The connection is thread-local because queued deployments finish on a
        worker thread. Nested use cases join the ambient transaction rather
        than committing a portion of their caller's work early.
        """
        if getattr(self._local, "connection", None) is not None:
            yield
            return
        with self.lock, self._connection() as connection:
            # Acquire the SQLite writer reservation at the Unit-of-Work
            # boundary, rather than discovering a competing writer halfway
            # through a multi-store use case.
            connection.execute("BEGIN IMMEDIATE")
            self._local.connection = connection
            try:
                yield
            finally:
                del self._local.connection

    def _initialize(self) -> None:
        with self.lock, self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(_SCHEMA)

    @staticmethod
    def now() -> str:
        return datetime.now(UTC).isoformat()


class AnsibleSettingStore:
    """Non-secret, fleet-wide Ansible policy stored by the control plane."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def list_settings(self) -> dict[str, Any]:
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT name, document FROM ansible_settings ORDER BY name"
            ).fetchall()
        return {row["name"]: json.loads(row["document"]) for row in rows}

    def set_setting(self, name: str, value: Any) -> None:
        document = json.dumps(value, separators=(",", ":"), sort_keys=True)
        with self._db.lock, self._db.connect() as connection:
            connection.execute(
                "INSERT INTO ansible_settings (name, document, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
                "document = excluded.document, updated_at = excluded.updated_at",
                (name, document, self._db.now()),
            )

    def delete_setting(self, name: str) -> None:
        with self._db.lock, self._db.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM ansible_settings WHERE name = ?", (name,)
            )
        if cursor.rowcount == 0:
            raise NotFoundError(f"Ansible setting {name!r} does not exist")


class EdgeStore:
    """The fleet, and the table the Ansible inventory plugin reads.

    Ordinary CRUD with one unusual obligation: every row is written with
    :data:`~blitzecdn.domain.edges.EDGE_SCHEMA_VERSION`, because a reader
    outside this process depends on it. See the note on the ``edges`` table.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    def list_edges(self) -> list[Edge]:
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT document FROM edges ORDER BY name"
            ).fetchall()
        return [
            Edge.model_validate_json(row["document"], context=STORED) for row in rows
        ]

    def get_edge(self, name: str) -> Edge:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT document FROM edges WHERE name = ?", (name,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"edge {name!r} does not exist")
        return Edge.model_validate_json(row["document"], context=STORED)

    def create_edge(self, edge: Edge) -> Edge:
        try:
            with self._db.lock, self._db.connect() as connection:
                connection.execute(
                    "INSERT INTO edges (name, schema_version, document, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        edge.name,
                        EDGE_SCHEMA_VERSION,
                        edge.model_dump_json(),
                        self._db.now(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"edge {edge.name!r} already exists") from exc
        return edge

    def replace_edge(self, edge: Edge, *, expected: Edge | None = None) -> Edge:
        with self._db.lock, self._db.connect() as connection:
            values = (
                EDGE_SCHEMA_VERSION,
                edge.model_dump_json(),
                self._db.now(),
                edge.name,
            )
            if expected is None:
                cursor = connection.execute(
                    "UPDATE edges SET schema_version = ?, document = ?, "
                    "updated_at = ? WHERE name = ?",
                    values,
                )
            else:
                cursor = connection.execute(
                    "UPDATE edges SET schema_version = ?, document = ?, "
                    "updated_at = ? WHERE name = ? AND document = ?",
                    (*values, expected.model_dump_json()),
                )
            if cursor.rowcount != 1:
                if expected is not None:
                    raise ConflictError(
                        f"edge {edge.name!r} changed while it was being edited"
                    )
                raise NotFoundError(f"edge {edge.name!r} does not exist")
        return edge

    def delete_edge(self, name: str) -> None:
        with self._db.lock, self._db.connect() as connection:
            cursor = connection.execute("DELETE FROM edges WHERE name = ?", (name,))
            if cursor.rowcount != 1:
                raise NotFoundError(f"edge {name!r} does not exist")


class SiteStore:
    """The derived virtual hosts.

    Nothing should write here except re-derivation from records: an edit made
    directly survives only until the next record change silently reverts it.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    def list_sites(self) -> list[CdnSite]:
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT document FROM sites ORDER BY name"
            ).fetchall()
        return [
            CdnSite.model_validate_json(row["document"], context=STORED) for row in rows
        ]

    def get_site(self, name: str) -> CdnSite:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT document FROM sites WHERE name = ?", (name,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"CDN site {name!r} does not exist")
        return CdnSite.model_validate_json(row["document"], context=STORED)

    def create_site(self, site: CdnSite) -> CdnSite:
        try:
            with self._db.lock, self._db.connect() as connection:
                connection.execute(
                    "INSERT INTO sites (name, document, updated_at) VALUES (?, ?, ?)",
                    (site.name, site.model_dump_json(), self._db.now()),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"CDN site {site.name!r} already exists") from exc
        return site

    def replace_site(self, site: CdnSite) -> CdnSite:
        with self._db.lock, self._db.connect() as connection:
            cursor = connection.execute(
                "UPDATE sites SET document = ?, updated_at = ? WHERE name = ?",
                (site.model_dump_json(), self._db.now(), site.name),
            )
            if cursor.rowcount != 1:
                raise NotFoundError(f"CDN site {site.name!r} does not exist")
        return site

    def delete_site(self, name: str) -> None:
        with self._db.lock, self._db.connect() as connection:
            cursor = connection.execute("DELETE FROM sites WHERE name = ?", (name,))
            if cursor.rowcount != 1:
                raise NotFoundError(f"CDN site {name!r} does not exist")

    def replace_all_sites(self, sites: list[CdnSite]) -> None:
        with self._db.lock, self._db.connect() as connection:
            connection.execute("DELETE FROM sites")
            connection.executemany(
                "INSERT INTO sites (name, document, updated_at) VALUES (?, ?, ?)",
                [(site.name, site.model_dump_json(), self._db.now()) for site in sites],
            )

    def projection_revision(self) -> str | None:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT source_revision FROM projection_state WHERE name = 'sites'"
            ).fetchone()
        return str(row["source_revision"]) if row else None

    def set_projection_revision(self, revision: str) -> None:
        with self._db.lock, self._db.connect() as connection:
            connection.execute(
                """INSERT INTO projection_state (name, source_revision, projected_at)
                   VALUES ('sites', ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                     source_revision = excluded.source_revision,
                     projected_at = excluded.projected_at""",
                (revision, self._db.now()),
            )


class ZoneStore:
    """Zones and their records — the source of truth sites derive from."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def list_domains(self) -> list[Domain]:
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT document FROM domains ORDER BY name"
            ).fetchall()
        return [Domain.model_validate_json(row["document"]) for row in rows]

    def get_domain(self, name: str) -> Domain:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT document FROM domains WHERE name = ?", (name,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"domain {name!r} does not exist")
        return Domain.model_validate_json(row["document"])

    def create_domain(self, domain: Domain) -> Domain:
        try:
            with self._db.lock, self._db.connect() as connection:
                connection.execute(
                    "INSERT INTO domains (name, document, updated_at) VALUES (?, ?, ?)",
                    (domain.name, domain.model_dump_json(), self._db.now()),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"domain {domain.name!r} already exists") from exc
        return domain

    def delete_domain(self, name: str) -> None:
        with self._db.lock, self._db.connect() as connection:
            cursor = connection.execute("DELETE FROM domains WHERE name = ?", (name,))
            if cursor.rowcount != 1:
                raise NotFoundError(f"domain {name!r} does not exist")

    def list_records(self, domain: str | None = None) -> list[DnsRecord]:
        query = "SELECT document FROM dns_records"
        parameters: tuple[str, ...] = ()
        if domain is not None:
            query += " WHERE domain = ?"
            parameters = (domain,)
        query += " ORDER BY domain, name, type"
        with self._db.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            DnsRecord.model_validate_json(row["document"], context=STORED)
            for row in rows
        ]

    def get_record(self, domain: str, name: str, type_: RecordType) -> DnsRecord:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT document FROM dns_records "
                "WHERE domain = ? AND name = ? AND type = ?",
                (domain, name, type_.value),
            ).fetchone()
        if row is None:
            raise NotFoundError(
                f"{type_.value} record {name!r} in {domain!r} does not exist"
            )
        return DnsRecord.model_validate_json(row["document"], context=STORED)

    def create_record(self, record: DnsRecord) -> DnsRecord:
        try:
            with self._db.lock, self._db.connect() as connection:
                connection.execute(
                    "INSERT INTO dns_records "
                    "(domain, name, type, document, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        record.domain,
                        record.name,
                        record.type.value,
                        record.model_dump_json(),
                        self._db.now(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            # A failed foreign key and a duplicate primary key both land here,
            # and the operator needs to know which.
            if "FOREIGN KEY" in str(exc).upper():
                raise NotFoundError(
                    f"domain {record.domain!r} does not exist; add it first"
                ) from exc
            raise ConflictError(
                f"{record.type.value} record {record.name!r} already exists "
                f"in {record.domain!r}"
            ) from exc
        return record

    def replace_record(
        self, record: DnsRecord, *, expected: DnsRecord | None = None
    ) -> DnsRecord:
        with self._db.lock, self._db.connect() as connection:
            values = (
                record.model_dump_json(),
                self._db.now(),
                record.domain,
                record.name,
                record.type.value,
            )
            if expected is None:
                cursor = connection.execute(
                    "UPDATE dns_records SET document = ?, updated_at = ? "
                    "WHERE domain = ? AND name = ? AND type = ?",
                    values,
                )
            else:
                cursor = connection.execute(
                    "UPDATE dns_records SET document = ?, updated_at = ? "
                    "WHERE domain = ? AND name = ? AND type = ? AND document = ?",
                    (*values, expected.model_dump_json()),
                )
            if cursor.rowcount != 1:
                if expected is not None:
                    raise ConflictError(
                        f"{record.type.value} record {record.name!r} changed "
                        "while it was being edited"
                    )
                raise NotFoundError(
                    f"{record.type.value} record {record.name!r} in "
                    f"{record.domain!r} does not exist"
                )
        return record

    def delete_record(self, domain: str, name: str, type_: RecordType) -> None:
        with self._db.lock, self._db.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM dns_records WHERE domain = ? AND name = ? AND type = ?",
                (domain, name, type_.value),
            )
            if cursor.rowcount != 1:
                raise NotFoundError(
                    f"{type_.value} record {name!r} in {domain!r} does not exist"
                )

    def replace_all_records(
        self, domains: list[Domain], records: list[DnsRecord]
    ) -> None:
        """Restore zones wholesale. Used by rollback."""
        with self._db.lock, self._db.connect() as connection:
            connection.execute("DELETE FROM dns_records")
            connection.execute("DELETE FROM domains")
            connection.executemany(
                "INSERT INTO domains (name, document, updated_at) VALUES (?, ?, ?)",
                [
                    (domain.name, domain.model_dump_json(), self._db.now())
                    for domain in domains
                ],
            )
            connection.executemany(
                "INSERT INTO dns_records "
                "(domain, name, type, document, updated_at) VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        record.domain,
                        record.name,
                        record.type.value,
                        record.model_dump_json(),
                        self._db.now(),
                    )
                    for record in records
                ],
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
    ) -> Deployment:
        deployment_id = uuid4().hex
        with self._db.lock, self._db.connect() as connection:
            connection.execute(
                """INSERT INTO deployments
                   (id,status,operator,check_mode,rollback_of,created_at,snapshot,
                    host_limit)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    deployment_id,
                    DeploymentStatus.QUEUED,
                    operator,
                    check_mode,
                    rollback_of,
                    self._db.now(),
                    snapshot or self._snapshot_source(),
                    host_limit,
                ),
            )
        return self.get_deployment(deployment_id)

    def transition(
        self,
        deployment_id: str,
        expected: DeploymentStatus,
        target: DeploymentStatus,
        **values: Any,
    ) -> Deployment:
        # The lifecycle table belongs to the domain and is enforced before the
        # SQL, so an illegal step is refused even when no row would match. The
        # `WHERE status = expected` guard then owns the race: a lawful step
        # against a stale expected state is a ConflictError, not a ValueError.
        require_transition(expected, target)
        allowed = {"started_at", "finished_at", "result"}
        if set(values) - allowed:
            raise ValueError("unsupported deployment transition fields")
        result = values.get("result")
        if result is not None and not isinstance(result, AnsibleRun):
            raise ValueError("deployment result must be an AnsibleRun")
        parameters = [
            target,
            values.get("started_at"),
            values.get("finished_at"),
            result.model_dump_json() if result is not None else None,
            deployment_id,
            expected,
        ]
        with self._db.lock, self._db.connect() as connection:
            cursor = connection.execute(
                """UPDATE deployments SET
                   status = ?,
                   started_at = COALESCE(?, started_at),
                   finished_at = COALESCE(?, finished_at),
                   result = COALESCE(?, result)
                   WHERE id = ? AND status = ?""",
                parameters,
            )
            if cursor.rowcount != 1:
                raise ConflictError(f"deployment {deployment_id} is not {expected}")
        return self.get_deployment(deployment_id)

    def get_deployment(self, deployment_id: str) -> Deployment:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM deployments WHERE id = ?", (deployment_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"deployment {deployment_id!r} does not exist")
        return self._deployment(row)

    def deployment_snapshot(self, deployment_id: str) -> str:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT snapshot FROM deployments WHERE id = ?", (deployment_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"deployment {deployment_id!r} does not exist")
        return str(row["snapshot"])

    def list_deployments(self, limit: int = 20) -> list[Deployment]:
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM deployments ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
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
        ).model_dump_json()
        with self._db.lock, self._db.connect() as connection:
            cursor = connection.execute(
                """UPDATE deployments
                   SET status = ?, finished_at = ?, result = ?
                   WHERE status IN (?, ?)""",
                (
                    DeploymentStatus.ABANDONED,
                    now,
                    abandoned,
                    DeploymentStatus.QUEUED,
                    DeploymentStatus.RUNNING,
                ),
            )
        return cursor.rowcount

    def successful_rollback_target(self, current_snapshot: str) -> Deployment:
        # `host_limit IS NULL` keeps canaries out of the automatic choice. A
        # limited run only proves one edge reached that snapshot, so rolling
        # the fleet back to it would converge most edges onto a state they
        # were never running. An operator can still name one explicitly.
        with self._db.connect() as connection:
            row = connection.execute(
                """SELECT * FROM deployments
                   WHERE status = ? AND check_mode = 0 AND snapshot != ?
                     AND host_limit IS NULL
                   ORDER BY created_at DESC LIMIT 1""",
                (DeploymentStatus.SUCCEEDED, current_snapshot),
            ).fetchone()
        if row is None:
            raise NotFoundError("no different successful deployment is available")
        return self._deployment(row)

    @staticmethod
    def _deployment(row: sqlite3.Row) -> Deployment:
        data = dict(row)
        data.pop("snapshot")
        stored = data.pop("result", None)
        data["result"] = json.loads(stored) if stored else None
        return Deployment.model_validate(data)


class AuditLog:
    """Append-only record of who did what."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def audit(
        self,
        operator: str,
        action: str,
        resource_type: str,
        resource_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        with self._db.lock, self._db.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO audit_events
                   (created_at,operator,action,resource_type,resource_id,details)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    self._db.now(),
                    operator,
                    action,
                    resource_type,
                    resource_id,
                    json.dumps(details or {}, sort_keys=True),
                ),
            )
            event_id = int(cursor.lastrowid or 0)
        return self.get_audit_event(event_id)

    def get_audit_event(self, event_id: int) -> AuditEvent:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM audit_events WHERE id = ?", (event_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"audit event {event_id} does not exist")
        return self._audit_event(row)

    def list_audit_events(self, limit: int = 100) -> list[AuditEvent]:
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._audit_event(row) for row in rows]

    @staticmethod
    def _audit_event(row: sqlite3.Row) -> AuditEvent:
        data = dict(row)
        data["details"] = json.loads(data["details"])
        return AuditEvent.model_validate(data)


class WorkflowStore:
    """Durable progress for work spanning SQLite and external systems."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def create(
        self,
        workflow_id: str,
        kind: WorkflowKind,
        operator: str,
        resource_id: str | None = None,
    ) -> Workflow:
        now = self._db.now()
        with self._db.lock, self._db.connect() as connection:
            connection.execute(
                """INSERT INTO workflows
                   (id,kind,resource_id,status,operator,created_at,updated_at,steps)
                   VALUES (?, ?, ?, ?, ?, ?, ?, '[]')""",
                (
                    workflow_id,
                    kind,
                    resource_id,
                    WorkflowStatus.PENDING,
                    operator,
                    now,
                    now,
                ),
            )
        return self.get(workflow_id)

    def advance(
        self,
        workflow_id: str,
        status: WorkflowStatus,
        *,
        step: WorkflowStep | None = None,
        error: str | None = None,
    ) -> Workflow:
        current = self.get(workflow_id)
        steps = (*current.steps, step) if step else current.steps
        with self._db.lock, self._db.connect() as connection:
            connection.execute(
                """UPDATE workflows
                   SET status = ?, updated_at = ?, steps = ?, error = ?
                   WHERE id = ?""",
                (
                    status,
                    self._db.now(),
                    json.dumps([item.model_dump(mode="json") for item in steps]),
                    error,
                    workflow_id,
                ),
            )
        return self.get(workflow_id)

    def get(self, workflow_id: str) -> Workflow:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"workflow {workflow_id!r} does not exist")
        data = dict(row)
        data["steps"] = json.loads(data["steps"])
        return Workflow.model_validate(data)

    def unfinished(self) -> list[Workflow]:
        with self._db.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM workflows
                   WHERE status IN (?, ?) ORDER BY created_at""",
                (WorkflowStatus.PENDING, WorkflowStatus.RUNNING),
            ).fetchall()
        return [self.get(str(row["id"])) for row in rows]

    def list_workflows(self, limit: int = 100) -> list[Workflow]:
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM workflows ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self.get(str(row["id"])) for row in rows]


class OutboxStore:
    """Transactional integration events with retry metadata."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def enqueue(self, topic: str, event_key: str, payload: dict[str, Any]) -> None:
        with self._db.lock, self._db.connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO outbox_events
                   (topic,event_key,payload,created_at) VALUES (?, ?, ?, ?)""",
                (topic, event_key, json.dumps(payload, sort_keys=True), self._db.now()),
            )

    def pending(self, limit: int = 100) -> list[OutboxEvent]:
        with self._db.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM outbox_events WHERE delivered_at IS NULL
                   ORDER BY id LIMIT ?""",
                (limit,),
            ).fetchall()
        return [self._event(row) for row in rows]

    def delivered(self, event_id: int) -> None:
        with self._db.lock, self._db.connect() as connection:
            connection.execute(
                """UPDATE outbox_events SET delivered_at = ?, attempts = attempts + 1,
                   last_error = NULL WHERE id = ? AND delivered_at IS NULL""",
                (self._db.now(), event_id),
            )

    def failed(self, event_id: int, error: str) -> None:
        with self._db.lock, self._db.connect() as connection:
            connection.execute(
                """UPDATE outbox_events SET attempts = attempts + 1, last_error = ?
                   WHERE id = ? AND delivered_at IS NULL""",
                (error, event_id),
            )

    @staticmethod
    def _event(row: sqlite3.Row) -> OutboxEvent:
        data = dict(row)
        data["payload"] = json.loads(data["payload"])
        return OutboxEvent.model_validate(data)
