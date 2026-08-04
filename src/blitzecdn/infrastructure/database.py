from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from blitzecdn.domain.models import AuditEvent, CdnSite, Deployment, DeploymentStatus
from blitzecdn.exceptions import ConflictError, NotFoundError


class Repository:
    """SQLite persistence with explicit transactions and immutable snapshots."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._initialize()

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

    def _initialize(self) -> None:
        with self._lock, self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sites (
                    name TEXT PRIMARY KEY,
                    document TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deployments (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    operator TEXT NOT NULL,
                    check_mode INTEGER NOT NULL,
                    rollback_of TEXT REFERENCES deployments(id),
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    return_code INTEGER,
                    stdout TEXT NOT NULL DEFAULT '',
                    stderr TEXT NOT NULL DEFAULT '',
                    snapshot TEXT NOT NULL
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
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def list_sites(self) -> list[CdnSite]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT document FROM sites ORDER BY name"
            ).fetchall()
        return [CdnSite.model_validate_json(row["document"]) for row in rows]

    def get_site(self, name: str) -> CdnSite:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT document FROM sites WHERE name = ?", (name,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"CDN site {name!r} does not exist")
        return CdnSite.model_validate_json(row["document"])

    def create_site(self, site: CdnSite) -> CdnSite:
        try:
            with self._lock, self._connection() as connection:
                connection.execute(
                    "INSERT INTO sites (name, document, updated_at) VALUES (?, ?, ?)",
                    (site.name, site.model_dump_json(), self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"CDN site {site.name!r} already exists") from exc
        return site

    def replace_site(self, site: CdnSite) -> CdnSite:
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "UPDATE sites SET document = ?, updated_at = ? WHERE name = ?",
                (site.model_dump_json(), self._now(), site.name),
            )
            if cursor.rowcount != 1:
                raise NotFoundError(f"CDN site {site.name!r} does not exist")
        return site

    def delete_site(self, name: str) -> None:
        with self._lock, self._connection() as connection:
            cursor = connection.execute("DELETE FROM sites WHERE name = ?", (name,))
            if cursor.rowcount != 1:
                raise NotFoundError(f"CDN site {name!r} does not exist")

    def replace_all_sites(self, sites: list[CdnSite]) -> None:
        with self._lock, self._connection() as connection:
            connection.execute("DELETE FROM sites")
            connection.executemany(
                "INSERT INTO sites (name, document, updated_at) VALUES (?, ?, ?)",
                [(site.name, site.model_dump_json(), self._now()) for site in sites],
            )

    def snapshot(self) -> str:
        return json.dumps(
            [site.model_dump(mode="json") for site in self.list_sites()], sort_keys=True
        )

    @staticmethod
    def decode_snapshot(snapshot: str) -> list[CdnSite]:
        data = json.loads(snapshot)
        if not isinstance(data, list):
            raise ValueError("deployment snapshot is not a list")
        return [CdnSite.model_validate(item) for item in data]

    def create_deployment(
        self,
        operator: str,
        *,
        check_mode: bool,
        rollback_of: str | None = None,
        snapshot: str | None = None,
    ) -> Deployment:
        deployment_id = uuid4().hex
        with self._lock, self._connection() as connection:
            connection.execute(
                """INSERT INTO deployments
                   (id,status,operator,check_mode,rollback_of,created_at,snapshot)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    deployment_id,
                    DeploymentStatus.QUEUED,
                    operator,
                    check_mode,
                    rollback_of,
                    self._now(),
                    snapshot or self.snapshot(),
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
        allowed = {"started_at", "finished_at", "return_code", "stdout", "stderr"}
        if set(values) - allowed:
            raise ValueError("unsupported deployment transition fields")
        parameters = [
            target,
            values.get("started_at"),
            values.get("finished_at"),
            values.get("return_code"),
            values.get("stdout"),
            values.get("stderr"),
            deployment_id,
            expected,
        ]
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """UPDATE deployments SET
                   status = ?,
                   started_at = COALESCE(?, started_at),
                   finished_at = COALESCE(?, finished_at),
                   return_code = COALESCE(?, return_code),
                   stdout = COALESCE(?, stdout),
                   stderr = COALESCE(?, stderr)
                   WHERE id = ? AND status = ?""",
                parameters,
            )
            if cursor.rowcount != 1:
                raise ConflictError(f"deployment {deployment_id} is not {expected}")
        return self.get_deployment(deployment_id)

    def get_deployment(self, deployment_id: str) -> Deployment:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM deployments WHERE id = ?", (deployment_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"deployment {deployment_id!r} does not exist")
        return self._deployment(row)

    def deployment_snapshot(self, deployment_id: str) -> str:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT snapshot FROM deployments WHERE id = ?", (deployment_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"deployment {deployment_id!r} does not exist")
        return str(row["snapshot"])

    def list_deployments(self, limit: int = 20) -> list[Deployment]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM deployments ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._deployment(row) for row in rows]

    def abandon_running(self) -> int:
        now = self._now()
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """UPDATE deployments
                   SET status = ?, finished_at = ?, stderr = ?
                   WHERE status IN (?, ?)""",
                (
                    DeploymentStatus.ABANDONED,
                    now,
                    "Controller restarted before completion",
                    DeploymentStatus.QUEUED,
                    DeploymentStatus.RUNNING,
                ),
            )
        return cursor.rowcount

    def successful_rollback_target(self, current_snapshot: str) -> Deployment:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT * FROM deployments
                   WHERE status = ? AND check_mode = 0 AND snapshot != ?
                   ORDER BY created_at DESC LIMIT 1""",
                (DeploymentStatus.SUCCEEDED, current_snapshot),
            ).fetchone()
        if row is None:
            raise NotFoundError("no different successful deployment is available")
        return self._deployment(row)

    def audit(
        self,
        operator: str,
        action: str,
        resource_type: str,
        resource_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """INSERT INTO audit_events
                   (created_at,operator,action,resource_type,resource_id,details)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    self._now(),
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
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM audit_events WHERE id = ?", (event_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"audit event {event_id} does not exist")
        return self._audit_event(row)

    def list_audit_events(self, limit: int = 100) -> list[AuditEvent]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._audit_event(row) for row in rows]

    @staticmethod
    def _deployment(row: sqlite3.Row) -> Deployment:
        data = dict(row)
        data.pop("snapshot")
        return Deployment.model_validate(data)

    @staticmethod
    def _audit_event(row: sqlite3.Row) -> AuditEvent:
        data = dict(row)
        data["details"] = json.loads(data["details"])
        return AuditEvent.model_validate(data)
