"""When a deployment pays off a durable requirement, apart from the run itself.

A requirement says current desired state still has to reach the fleet. Only a
forward, fleet-wide, applied run compiled after the requirement was raised
delivers that: a canary leaves other edges behind, a rollback restores a release
that may predate the material, and an earlier queued run compiled older state.
Functions over the service's ports; the service keeps the lock and the
transaction.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from blitzecdn.capabilities.deployments.domain import (
    Deployment,
    DeploymentRequirementKind,
)
from blitzecdn.capabilities.deployments.ports import DeploymentRequirements

__all__ = ["queued_to_satisfy", "satisfies", "settle"]


def satisfies(deployment: Deployment) -> bool:
    """Whether success means current state reached every edge: a forward fleet run."""
    return (
        not deployment.check_mode
        and deployment.host_limit is None
        and deployment.rollback_of is None
    )


def queued_to_satisfy(
    queued: Iterable[Deployment], requested_at: datetime
) -> Deployment | None:
    """A queued deployment that will satisfy a requirement raised at ``requested_at``.

    Its release is compiled at admission, so one queued before the requirement
    carries older state and cannot.
    """
    for waiting in queued:
        if satisfies(waiting) and waiting.created_at >= requested_at:
            return waiting
    return None


def settle(requirements: DeploymentRequirements, deployment: Deployment) -> None:
    """Clear what a succeeded ``deployment`` delivered, inside its transaction."""
    if satisfies(deployment):
        requirements.clear(
            DeploymentRequirementKind.CERTIFICATES,
            # The release was compiled under the lock before this row, so only a
            # requirement raised before it is in the release.
            through=deployment.created_at,
        )
