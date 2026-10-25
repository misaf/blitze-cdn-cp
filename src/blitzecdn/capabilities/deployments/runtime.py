"""What the deployments capability runs: recovery at startup and drift checks.

Republishing queued deployments and marking orphaned runs abandoned has to
happen while holding the fleet-wide deployment lock, and once per node. That is
what a feature's startup is for: it runs when the API process starts, never
from a CLI invocation, which would take that lock on every command an operator
typed.
"""

from __future__ import annotations

from blitzecdn.capabilities.deployments.service.convergence import DeploymentService
from blitzecdn.core.plugins import FeatureOperations, ScheduledJob


def deployments_operations(
    *, deployments: DeploymentService, drift_check_interval_seconds: int
) -> FeatureOperations:
    """The drift check, and node-startup recovery of interrupted deployments."""

    def check(operator: str) -> None:
        deployments.check_drift(operator)

    def startup() -> None:
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
