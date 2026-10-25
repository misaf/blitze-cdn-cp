"""A deployment: the run, its status, its transitions, and what it found.

What it converged to is a release, which it names by identifier only.
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
    abandoned_run,
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
    "abandoned_run",
    "aborted_run",
    "is_terminal",
    "require_transition",
]
