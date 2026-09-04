"""How the maintenance capability is built.

The same shape as :mod:`blitzecdn.capabilities.sites.composition`, with the one
argument that is a closure rather than a store.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from blitzecdn.capabilities.maintenance.ports import Requirements
from blitzecdn.capabilities.maintenance.service import MaintenanceService

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from blitzecdn.composition import ControlPlane

__all__ = ["build_maintenance_service"]


def build_maintenance_service(
    platform: ControlPlane, *, requirements: Requirements
) -> MaintenanceService:
    """Wire the runner of scheduled jobs, and of the convergence they ask for.

    The job table is passed as a callable and not as the table itself. This
    service is one of the things the composition root builds, and the table is
    resolved from plugins that are handed the finished control plane — so
    reading ``platform.jobs`` here would resolve a list that cannot yet include
    a job contributed by a plugin this build has not reached. ``JobTable`` says
    so in its own words; this is the call site it was written for.
    """
    return MaintenanceService(
        jobs=lambda: platform.jobs,
        deployments=platform.deployments,
        requirements=requirements,
    )
