"""One SQLite database, exposed as focused capability stores.

``Repository`` opens the database and hands out the focused stores that sit on
it. It is a bundle, not a layer: each store already satisfies its port in
capability ports structurally, so the composition root passes
``repository.zones`` to whatever asked for a ``ZoneStore`` and no service is
ever handed more of persistence than it declared.

It lives here rather than in ``core.persistence`` because choosing which
stores go on one database is composition, and it was the last thing under
``core`` that imported a capability to do its job. What stays in
``core.persistence`` is what a capability builds *on* — the engine, the write
lock, the Unit of Work, the schema — and what core itself keeps there: the
audit log and the workflow journal.

It was ``blitzecdn.persistence``, one import away from
``blitzecdn.core.persistence`` and spending this paragraph saying which of the
two it was. The directory says it now.

``snapshot`` is the one thing that cannot belong to a single store, because the
desired state a deployment converges spans the zones, their records, and the
sites those records route to.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path

from blitzecdn.capabilities.deployments.adapters.persistence import (
    DeploymentRequirementStore,
    DeploymentStore,
)
from blitzecdn.capabilities.deployments.domain.snapshots import encode_snapshot
from blitzecdn.capabilities.dns.adapters.persistence import ZoneStore
from blitzecdn.capabilities.edges.adapters.persistence import EdgeStore
from blitzecdn.capabilities.sites.adapters.persistence import SiteStore
from blitzecdn.capabilities.workflows.adapters.persistence import WorkflowStore
from blitzecdn.core.persistence.audit import AuditLog
from blitzecdn.core.persistence.configuration import AnsibleSettingStore
from blitzecdn.core.persistence.engine import Database

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

    A composition of the capability stores, and nothing more. Reach through it
    for the store you want —
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
        self.deployment_requirements = DeploymentRequirementStore(self.database)
        self.audit_log = AuditLog(self.database)
        self.workflows = WorkflowStore(self.database)

    def snapshot(self) -> str:
        """Serialise the desired state a deployment converges and can roll back to.

        Spans three tables, so it belongs to the bundle rather than to any one
        store. ``DeploymentStore`` is handed this bound method at construction:
        it records a snapshot with every deployment without knowing what a
        snapshot contains.

        Sites are read here rather than derived on the way out. They stopped
        being derivable when they stopped being a projection of the records,
        and a site no record routes to yet is desired state all the same.
        """
        with self.transaction():
            domains = self.zones.list_domains()
            records = self.zones.list_records()
            sites = self.sites.list_sites()
            return encode_snapshot(domains, records, sites)

    def transaction(self) -> AbstractContextManager[None]:
        """Open the Unit of Work shared by this repository's stores."""
        return self.database.transaction()

    def close(self) -> None:
        """Release persistence resources owned by this repository."""
        self.database.close()
