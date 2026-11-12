"""The HTTP representations this capability publishes, and the bodies it takes.

Other capabilities do not import these: one that answers with a deployment it
started defines its own representation of it, so a change here stays here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar, Literal

from pydantic import Field, model_validator

from blitzecdn.capabilities.deployments.domain import (
    DeploymentStatus,
    TargetPhase,
    TargetStatus,
)
from blitzecdn.core.domain.configuration import ConfigurationChangeKind
from blitzecdn.delivery.api.models import AnsibleRun, HostRun, Model, QueuedWork
from blitzecdn.delivery.api.requests import FleetRequest, RequestModel


class Deployment(QueuedWork):
    """A convergence, or a rollback when ``rollback_of`` names the one it repeats.

    ``kind`` is ``rollback`` for a rollback and ``deployment`` otherwise; with
    ``check_mode`` either is a drift check.
    """

    ACTIVE_STATUSES: ClassVar[frozenset[str]] = frozenset(
        {DeploymentStatus.QUEUED, DeploymentStatus.RUNNING}
    )

    kind: Literal["deployment", "rollback"]
    status: DeploymentStatus
    operator: str
    check_mode: bool
    #: The converged release; follow ``GET /v3/releases/{id}`` for its artifacts.
    release_id: str
    #: What the fleet ran when this was queued; ``GET .../diff`` compares the two.
    previous_release_id: str | None = None
    host_limit: str | None = None
    rollback_of: str | None = None
    canonical_digest: str | None = None
    started_at: datetime | None = None
    result: AnsibleRun | None = None

    @model_validator(mode="before")
    @classmethod
    def _derive_kind(cls, data: Any) -> Any:
        if isinstance(data, dict) and "kind" not in data:
            kind = "rollback" if data.get("rollback_of") else "deployment"
            return {**data, "kind": kind}
        return data


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


class DeploymentChange(Model):
    """One rendered path that differs; ``diff`` is ``None`` for non-text content."""

    path: str
    change: ConfigurationChangeKind
    diff: str | None = None


class DeploymentDiff(Model):
    """What a deployment changed on the edges, file by file.

    ``before`` and ``after`` are the digests of the configuration the fleet ran
    when it was queued and the one it converges; ``before`` is ``None`` when
    nothing had reached an edge yet, so every file is added.
    """

    deployment_id: str
    previous_release_id: str | None = None
    release_id: str
    changed: bool
    before: str | None = None
    after: str
    changes: tuple[DeploymentChange, ...] = ()


class DeployRequest(FleetRequest):
    check: bool = Field(
        default=False,
        description="False applies changes; true uses Ansible check mode.",
    )


class RollbackRequest(RequestModel):
    deployment_id: str | None = Field(default=None, min_length=32, max_length=32)
    check: bool = False


__all__ = [
    "DeployRequest",
    "Deployment",
    "DeploymentChange",
    "DeploymentDiff",
    "DeploymentTarget",
    "DriftReport",
    "RollbackRequest",
]
