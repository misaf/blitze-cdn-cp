"""A deployment: the run, its status, its transitions, and what it found.

What it converged *to* is not here. That is a release, and it belongs to the
capability that compiles one — a deployment holds the identifier and nothing
more. The desired state used to live in this package as `snapshots.py`, which
made a value spanning three other capabilities' models the property of the one
that happened to take it, and left "what should the fleet serve" answerable
only by a caller that already had a deployment in hand.
"""

from blitzecdn.capabilities.deployments.domain.deployment import (
    DEPLOYMENT_TRANSITIONS,
    DEPLOYMENT_WORKFLOW,
    ROLLBACK_WORKFLOW,
    TERMINAL_STATUSES,
    Deployment,
    DeploymentRequirementKind,
    DeploymentStatus,
    DriftReport,
    aborted_run,
    is_terminal,
    require_transition,
)
from blitzecdn.capabilities.deployments.domain.target import (
    PHASE_ORDER,
    DeploymentTarget,
    TargetPhase,
    TargetStatus,
)

__all__ = [
    "DEPLOYMENT_TRANSITIONS",
    "DEPLOYMENT_WORKFLOW",
    "PHASE_ORDER",
    "ROLLBACK_WORKFLOW",
    "TERMINAL_STATUSES",
    "Deployment",
    "DeploymentRequirementKind",
    "DeploymentStatus",
    "DeploymentTarget",
    "DriftReport",
    "TargetPhase",
    "TargetStatus",
    "aborted_run",
    "is_terminal",
    "require_transition",
]
