"""The HTTP representations this capability publishes, and the bodies it takes.

``Deployment`` is this capability's shape even though `certificates` returns
one too: a renewal that triggers a convergence answers with what
`deployments` published, imported from here rather than restated. The result
inside it is core's `AnsibleRun`, and the durable progress a caller polls is
core's `Workflow` — a deployment is one kind of workflow, not the definition
of one.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from blitzecdn.api.models import AnsibleRun, HostRun, Model
from blitzecdn.api.requests import FleetRequest, RequestModel
from blitzecdn.capabilities.deployments.domain import (
    DeploymentStatus,
    TargetPhase,
    TargetStatus,
)


class Deployment(Model):
    id: str
    status: DeploymentStatus
    operator: str
    check_mode: bool
    #: The compiled release this run converged. The link between "what did we
    #: deploy" and "what does that mean", which a client follows to
    #: ``GET /v1/releases/{id}`` for the artifact digests and the explanation.
    release_id: str
    host_limit: str | None = None
    rollback_of: str | None = None
    canonical_digest: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: AnsibleRun | None = None


class DeploymentTarget(Model):
    """One edge's progress through one deployment.

    Published because the question it answers has no other home: an operator
    or a dashboard looking at a failed fleet deploy needs to know which edges
    are on the new configuration and which are still on the old, and a
    fleet-wide result can only report the second as an absence.
    """

    edge: str
    status: TargetStatus
    phase: TargetPhase | None = None
    attempts: int
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_error: str | None = None


class DriftReport(Model):
    deployment_id: str
    checked_at: datetime
    host_limit: str | None = None
    hosts: tuple[HostRun, ...] = ()
    unattempted: tuple[str, ...] = ()


class DeployRequest(FleetRequest):
    check: bool = False


class DriftRequest(FleetRequest):
    pass


class RollbackRequest(RequestModel):
    deployment_id: str | None = Field(default=None, min_length=32, max_length=32)
    check: bool = False


__all__ = [
    "DeployRequest",
    "Deployment",
    "DeploymentTarget",
    "DriftReport",
    "DriftRequest",
    "RollbackRequest",
]
