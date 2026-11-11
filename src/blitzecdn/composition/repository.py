"""The one SQLite database, and the stores no single capability owns.

Each capability builds its own stores on this ``database`` in its
`wiring.py` provider. What stays here is cross-cutting: the engine, the Unit of
Work every store shares, the audit log every service writes, and the fleet
settings an operator overrides, and the
idempotency keys every queued API route consults.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path

from blitzecdn.core.persistence.audit import AuditLog
from blitzecdn.core.persistence.configuration import AnsibleSettingStore
from blitzecdn.core.persistence.engine import Database
from blitzecdn.core.persistence.idempotency import IdempotencyKeys
from blitzecdn.core.ports.clock import Clock, system_clock

__all__ = ["AuditLog", "Database", "IdempotencyKeys", "Repository"]


class Repository:
    """The database, its Unit of Work, and the stores every capability shares."""

    def __init__(
        self,
        path: Path,
        *,
        pool_connections: bool = False,
        audit_retention: int | None = None,
        clock: Clock = system_clock,
    ) -> None:
        self.database = Database(path, pool_connections=pool_connections, clock=clock)
        self.ansible_settings = AnsibleSettingStore(self.database)
        # The audit bound goes on the store: every service writes audit
        # events, so no single caller could carry it.
        self.audit_log = (
            AuditLog(self.database, audit_retention)
            if audit_retention is not None
            else AuditLog(self.database)
        )
        self.idempotency_keys = IdempotencyKeys(self.database)

    def transaction(self) -> AbstractContextManager[None]:
        """Open the Unit of Work shared by every store on this database."""
        return self.database.transaction()

    def close(self) -> None:
        """Release persistence resources owned by this repository."""
        self.database.close()
