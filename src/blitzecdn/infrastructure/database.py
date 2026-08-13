"""One SQLite database, exposed as four focused stores.

``Repository`` opens the database and hands out the four stores that sit on it.
It is a bundle, not a layer: each store already satisfies its port in
:mod:`blitzecdn.ports` structurally, so the composition root passes
``repository.zones`` to whatever asked for a ``ZoneStore`` and no service is
ever handed more of persistence than it declared.

``snapshot`` is the one thing that cannot belong to a single store, because the
desired state a deployment converges spans both zones and records.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path

from blitzecdn.domain.snapshots import encode_snapshot
from blitzecdn.infrastructure.engine import Database
from blitzecdn.infrastructure.stores import (
    AnsibleSettingStore,
    AuditLog,
    DeploymentStore,
    EdgeStore,
    SiteStore,
    WorkflowStore,
    ZoneStore,
)

__all__ = [
    "AuditLog",
    "Database",
    "DeploymentStore",
    "EdgeStore",
    "Repository",
    "SiteStore",
    "ZoneStore",
]


class Repository:
    """SQLite persistence with explicit transactions and immutable snapshots.

    A composition of the stores in :mod:`blitzecdn.infrastructure.stores`, and
    nothing more. Reach through it for the store you want —
    ``repository.zones.list_records()`` — rather than expecting a method here;
    forwarding every store method onto this class only made ``Repository`` look
    like the whole of persistence to callers that needed one table of it.
    """

    def __init__(self, path: Path, *, pool_connections: bool = False) -> None:
        self.database = Database(path, pool_connections=pool_connections)
        self.sites = SiteStore(self.database)
        self.zones = ZoneStore(self.database)
        self.edges = EdgeStore(self.database)
        self.ansible_settings = AnsibleSettingStore(self.database)
        self.deployments = DeploymentStore(self.database, self.snapshot)
        self.audit_log = AuditLog(self.database)
        self.workflows = WorkflowStore(self.database)

    def snapshot(self) -> str:
        """Serialise the desired state a deployment converges and can roll back to.

        Spans three tables, so it belongs to the bundle rather than to any one
        store. ``DeploymentStore`` is handed this bound method at construction:
        it records a snapshot with every deployment without knowing what a
        snapshot contains.
        """
        with self.transaction():
            domains = self.zones.list_domains()
            records = self.zones.list_records()
            return encode_snapshot(domains, records)

    def transaction(self) -> AbstractContextManager[None]:
        """Open the Unit of Work shared by this repository's stores."""
        return self.database.transaction()

    def close(self) -> None:
        """Release persistence resources owned by this repository."""
        self.database.close()
