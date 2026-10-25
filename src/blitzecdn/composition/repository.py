"""One SQLite database, exposed as focused capability stores.

A bundle, not a layer: each store satisfies its port, and services get only
the store they declared. Choosing which stores share the database is
composition, so it lives here; core keeps the engine, schema and its own stores.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path

from blitzecdn.capabilities.deployments.adapters.persistence import (
    DeploymentRequirementStore,
    DeploymentStore,
    DeploymentTargetStore,
)
from blitzecdn.capabilities.dns.adapters.persistence import ZoneStore
from blitzecdn.capabilities.dns.adapters.rules import RuleStore
from blitzecdn.capabilities.edges.adapters.persistence import EdgeStore
from blitzecdn.capabilities.jobs.adapters.persistence import JobStore, ScheduleStore
from blitzecdn.capabilities.releases.adapters.persistence import ReleaseStore
from blitzecdn.capabilities.releases.domain import ReleaseInputs
from blitzecdn.capabilities.workflows.adapters.persistence import WorkflowStore
from blitzecdn.core.persistence.audit import AuditLog
from blitzecdn.core.persistence.configuration import AnsibleSettingStore
from blitzecdn.core.persistence.engine import Database
from blitzecdn.core.ports.clock import Clock, system_clock

__all__ = [
    "AuditLog",
    "Database",
    "DeploymentStore",
    "EdgeStore",
    "ReleaseStore",
    "Repository",
    "RuleStore",
    "ZoneStore",
]


class Repository:
    """The capability stores on one database; reach through it for one."""

    def __init__(
        self,
        path: Path,
        *,
        pool_connections: bool = False,
        audit_retention: int | None = None,
        clock: Clock = system_clock,
    ) -> None:
        self.database = Database(path, pool_connections=pool_connections, clock=clock)
        self.zones = ZoneStore(self.database)
        self.rules = RuleStore(self.database)
        self.edges = EdgeStore(self.database)
        self.jobs = JobStore(self.database)
        self.schedules = ScheduleStore(self.database)
        self.ansible_settings = AnsibleSettingStore(self.database)
        self.deployments = DeploymentStore(self.database)
        self.deployment_requirements = DeploymentRequirementStore(self.database)
        self.deployment_targets = DeploymentTargetStore(self.database)
        # Release pruning asks the deployments store which releases are
        # still rollback targets, through the port it declared.
        self.releases = ReleaseStore(self.database, self.deployments)
        # The audit bound goes on the store: every service writes audit
        # events, so no single caller could carry it.
        self.audit_log = (
            AuditLog(self.database, audit_retention)
            if audit_retention is not None
            else AuditLog(self.database)
        )
        self.workflows = WorkflowStore(self.database)

    def release_inputs(self) -> ReleaseInputs:
        """The canonical state a release is compiled from, read in one transaction.

        Spans four tables, so it belongs to the bundle. Sites are derived, not read.
        """
        with self.transaction():
            return ReleaseInputs.of(
                self.zones.list_domains(),
                self.zones.list_records(),
                self.rules.list_rules(),
                settings=self.ansible_settings.list_settings(),
            )

    def transaction(self) -> AbstractContextManager[None]:
        """Open the Unit of Work shared by this repository's stores."""
        return self.database.transaction()

    def close(self) -> None:
        """Release persistence resources owned by this repository."""
        self.database.close()
