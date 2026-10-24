"""What the deployments capability runs: recovery at startup and drift checks.

Republishing queued deployments and marking orphaned runs abandoned has to
happen while holding the fleet-wide deployment lock, and once per node — so it
belongs to the API, which is the process that lives as long as the node does.
Doing it from every CLI invocation would take that lock on every command an
operator typed.
"""

from __future__ import annotations

from blitzecdn.capabilities.deployments.service.convergence import DeploymentService
from blitzecdn.core.plugins import FeatureOperations, ProcessKind, ScheduledJob


def deployments_operations(
    *, deployments: DeploymentService, drift_check_interval_seconds: int
) -> FeatureOperations:
    """The drift check, and API-startup recovery of interrupted deployments."""

    def check(operator: str) -> None:
        deployments.check_drift(operator)

    def startup(process: ProcessKind) -> None:
        if process is ProcessKind.API:
            deployments.initialize()

    return FeatureOperations(
        plugin="deployments",
        scheduled_jobs=(
            ScheduledJob(
                plugin="deployments",
                name="check-drift",
                interval_seconds=drift_check_interval_seconds,
                run=check,
                jitter_seconds=min(600, drift_check_interval_seconds // 10),
            ),
        ),
        startup=startup,
    )
