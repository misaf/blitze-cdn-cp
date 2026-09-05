"""A deployment, and the desired state one converges.

`deployment.py` is the run — its status, its transitions, what a drift check
found. `snapshots.py` is what it converged to: the zones, their records and the
sites those records route to, encoded as one document a rollback can read back.

A snapshot spans three capabilities' models and is still `deployments`' own
value, because a deployment is the only thing that takes one. It was a
top-level module here and had to be named in the layering test twice over —
once as a domain file, once as a module other capabilities may import.
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

__all__ = [
    "DEPLOYMENT_TRANSITIONS",
    "DEPLOYMENT_WORKFLOW",
    "ROLLBACK_WORKFLOW",
    "TERMINAL_STATUSES",
    "Deployment",
    "DeploymentRequirementKind",
    "DeploymentStatus",
    "DriftReport",
    "aborted_run",
    "is_terminal",
    "require_transition",
]
