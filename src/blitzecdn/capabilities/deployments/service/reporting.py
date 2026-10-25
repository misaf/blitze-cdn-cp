"""Reading a recorded deployment as an answer to a question.

Which runs count, and what a result is evidence of. Functions over
``DeploymentStore``: no lock, no transitions, no Ansible.
"""

from __future__ import annotations

from blitzecdn.capabilities.deployments.domain import (
    DriftReport,
    TargetStatus,
)
from blitzecdn.capabilities.deployments.ports import (
    DeploymentStore,
    DeploymentTargets,
    Releases,
)
from blitzecdn.core.exceptions import ConflictError

__all__ = ["drift_report", "site_is_deployed"]


def drift_report(
    deployments: DeploymentStore,
    targets: DeploymentTargets,
    deployment_id: str,
) -> DriftReport:
    """Read a recorded check-mode run as a drift report, live or revisited later."""
    deployment = deployments.get_deployment(deployment_id)
    if not deployment.check_mode:
        raise ConflictError(
            f"deployment {deployment.id} applied changes rather than "
            "previewing them, so its result describes what it did, not "
            "what had drifted. Run 'blitzecdn drift' instead."
        )
    # Edges after a failure are SKIPPED: unanswered, so not in sync.
    unreached = tuple(
        target.edge
        for target in targets.targets(deployment_id)
        if target.status in (TargetStatus.PENDING, TargetStatus.SKIPPED)
    )
    return DriftReport.of(deployment, unreached)


def site_is_deployed(
    deployments: DeploymentStore, releases: Releases, site_name: str
) -> bool:
    """Whether the newest successful real deployment carried this site.

    Check-mode runs prove nothing about serving and are skipped. A canary counts,
    so this is necessary, not sufficient.
    """
    deployment = deployments.latest_successful_applied()
    if deployment is None:
        return False
    release = releases.get(deployment.release_id)
    # What that run carried, not a re-derivation from today's zones.
    return any(entry.name == site_name for entry in release.sites)
