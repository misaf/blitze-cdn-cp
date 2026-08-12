"""One SQLite database, exposed as four focused stores.

``Repository`` opens the database and hands out the four stores that sit on it.
It is a bundle, not a layer: each store already satisfies its port in
:mod:`blitzecdn.ports` structurally, so the composition root passes
``repository.zones`` to whatever asked for a ``ZoneStore`` and no service is
ever handed more of persistence than it declared.

``snapshot`` is the one thing that cannot belong to a single store, because the
desired state a deployment converges spans zones, records and derived sites.
"""

from __future__ import annotations

from pathlib import Path

from blitzecdn.domain.snapshots import (
    SNAPSHOT_VERSION,
    decode_snapshot,
    decode_snapshot_zones,
    encode_snapshot,
)
from blitzecdn.infrastructure.stores import (
    AuditLog,
    Database,
    DeploymentStore,
    EdgeStore,
    SiteStore,
    ZoneStore,
)

__all__ = [
    "SNAPSHOT_VERSION",
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

    def __init__(self, path: Path) -> None:
        self.database = Database(path)
        self.sites = SiteStore(self.database)
        self.zones = ZoneStore(self.database)
        self.edges = EdgeStore(self.database)
        self.deployments = DeploymentStore(self.database, self.snapshot)
        self.audit_log = AuditLog(self.database)

    def snapshot(self) -> str:
        """Serialise the desired state a deployment converges and can roll back to.

        Spans three tables, so it belongs to the bundle rather than to any one
        store. ``DeploymentStore`` is handed this bound method at construction:
        it records a snapshot with every deployment without knowing what a
        snapshot contains.
        """
        return encode_snapshot(
            self.zones.list_domains(),
            self.zones.list_records(),
            self.sites.list_sites(),
        )

    decode_snapshot = staticmethod(decode_snapshot)
    decode_snapshot_zones = staticmethod(decode_snapshot_zones)
