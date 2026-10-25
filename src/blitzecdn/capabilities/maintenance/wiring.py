"""How the maintenance capability is built."""

from __future__ import annotations

from blitzecdn.capabilities.maintenance.ports import Deployments, JobTable
from blitzecdn.capabilities.maintenance.service import MaintenanceService

__all__ = ["build_maintenance_service"]


def build_maintenance_service(
    *, jobs: JobTable, deployments: Deployments
) -> MaintenanceService:
    """Wire the job runner; ``jobs`` is resolved after every service exists."""
    return MaintenanceService(jobs=jobs, deployments=deployments)
