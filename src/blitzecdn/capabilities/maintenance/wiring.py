"""How the maintenance capability is built.

The same shape as :mod:`blitzecdn.capabilities.dns.wiring`.
"""

from __future__ import annotations

from blitzecdn.capabilities.maintenance.ports import Deployments, JobTable
from blitzecdn.capabilities.maintenance.service import MaintenanceService

__all__ = ["build_maintenance_service"]


def build_maintenance_service(
    *, jobs: JobTable, deployments: Deployments
) -> MaintenanceService:
    """Wire the runner of scheduled jobs, and of the convergence they ask for.

    ``jobs`` is the resolved table. The composition root resolves it once,
    after every service a contributed job closes over has been built.
    """
    return MaintenanceService(jobs=jobs, deployments=deployments)
