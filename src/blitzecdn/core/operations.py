"""Intent and durable progress for long-running control-plane operations."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
DeploymentId = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]
EdgeName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]+$")]
Operator = Identifier


class WorkflowKind(StrEnum):
    DEPLOYMENT = "deployment"
    ROLLBACK = "rollback"
    CERTIFICATE = "certificate"


class MaintenanceOperation(StrEnum):
    RECONCILE_CERTIFICATES = "reconcile-certificates"
    RECONCILE_AUTOMATIC_SSL = "reconcile-automatic-ssl"
    RENEW_CERTIFICATES = "renew-certificates"
    CHECK_DRIFT = "check-drift"


class WorkflowStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"


class WorkflowStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    completed_at: datetime
    details: dict[str, Any] = Field(default_factory=dict)


class Workflow(BaseModel):
    """Crash-visible progress for an operation that crosses transaction systems."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    kind: WorkflowKind
    resource_id: str | None = None
    status: WorkflowStatus
    operator: Operator
    created_at: datetime
    updated_at: datetime
    steps: tuple[WorkflowStep, ...] = ()
    error: str | None = None

    @model_validator(mode="after")
    def terminal_workflow_has_an_outcome(self) -> Workflow:
        if self.status is WorkflowStatus.FAILED and not self.error:
            raise ValueError("a failed workflow must explain its failure")
        return self
