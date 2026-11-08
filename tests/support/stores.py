"""Every store on the test database, for seeding and reading state back.

Production builds each capability's stores in that capability's provider and
hands them only to its services. A test reaches past them, so this bundle builds
them all on one :class:`Repository`'s database. Stores hold no state of their
own, so these and the container's see the same rows.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Self

from blitzecdn.capabilities.deployments.adapters.persistence import (
    DeploymentOutcomeStore,
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
from blitzecdn.capabilities.tls.adapters.persistence import PreflightAttemptStore
from blitzecdn.capabilities.traffic.adapters.persistence import TrafficStore
from blitzecdn.capabilities.workflows.adapters.persistence import WorkflowStore
from blitzecdn.composition import Repository

__all__ = ["Stores"]


class Stores(Repository):
    """A :class:`Repository` that also exposes every capability's store."""

    def __init__(self, path: Path, **options: Any) -> None:
        super().__init__(path, **options)
        self._build_stores()

    @classmethod
    def sharing(cls, repository: Repository) -> Self:
        """The stores on ``repository``'s database, without opening another."""
        if isinstance(repository, cls):
            return repository
        stores = cls.__new__(cls)
        stores.database = repository.database
        stores.ansible_settings = repository.ansible_settings
        stores.audit_log = repository.audit_log
        stores._build_stores()
        return stores

    def _build_stores(self) -> None:
        database = self.database
        self.zones = ZoneStore(database)
        self.rules = RuleStore(database)
        self.edges = EdgeStore(database)
        self.jobs = JobStore(database)
        self.schedules = ScheduleStore(database)
        self.deployments = DeploymentStore(database)
        self.deployment_requirements = DeploymentRequirementStore(database)
        self.deployment_outcomes = DeploymentOutcomeStore(database)
        self.deployment_targets = DeploymentTargetStore(database)
        self.releases = ReleaseStore(database, self.deployments)
        self.workflows = WorkflowStore(database)
        self.traffic = TrafficStore(database)
        self.preflight_attempts = PreflightAttemptStore(database)

    def release_inputs(self) -> ReleaseInputs:
        """The canonical state a release compiles, read in one transaction."""
        with self.transaction():
            return ReleaseInputs.of(
                self.zones.list_domains(),
                self.zones.list_records(),
                self.rules.list_rules(),
                settings=self.ansible_settings.list_settings(),
            )
