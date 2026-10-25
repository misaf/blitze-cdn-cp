"""The HTTP representations this capability publishes, and the bodies it takes.

``Deployment`` is reused by other capabilities that answer with a convergence.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from blitzecdn.capabilities.deployments.domain import (
    DeploymentStatus,
    TargetPhase,
    TargetStatus,
)
from blitzecdn.entrypoints.api.models import AnsibleRun, HostRun, Model
from blitzecdn.entrypoints.api.requests import FleetRequest, RequestModel


class Deployment(Model):
    id: str
    status: DeploymentStatus
    operator: str
    check_mode: bool
    #: The converged release; follow ``GET /v1/releases/{id}`` for its artifacts.
    release_id: str
    host_limit: str | None = None
    rollback_of: str | None = None
    canonical_digest: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: AnsibleRun | None = None
    error: str | None = None


class DeploymentTarget(Model):
    """One edge's progress through one deployment: which edges are on which config."""

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
