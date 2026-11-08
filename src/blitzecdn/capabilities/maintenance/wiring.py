"""How the maintenance capability is built."""

from __future__ import annotations

from dishka import Provider, Scope, provide

from blitzecdn.capabilities.maintenance.ports import Deployments, JobTable
from blitzecdn.capabilities.maintenance.service import MaintenanceService
from blitzecdn.core.features import Operations

__all__ = ["MaintenanceProvider", "build_maintenance_service"]


def build_maintenance_service(
    *, jobs: JobTable, deployments: Deployments
) -> MaintenanceService:
    """Wire the job runner; ``jobs`` is resolved after every service exists."""
    return MaintenanceService(jobs=jobs, deployments=deployments)


class MaintenanceProvider(Provider):
    """Runs a contributed scheduled job by name."""

    scope = Scope.APP

    @provide
    def service(
        self, operations: Operations, deployments: Deployments
    ) -> MaintenanceService:
        return build_maintenance_service(
            jobs=operations.scheduled_jobs, deployments=deployments
        )
