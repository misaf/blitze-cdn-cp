"""Intent and durable progress for long-running control-plane operations."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
SiteName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]*$")]
DeploymentId = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]
EdgeName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]+$")]
Operator = Identifier


class DeploymentMode(StrEnum):
    APPLY = "apply"
    CHECK = "check"


class FleetTarget(BaseModel):
    """The whole fleet or a validated Ansible host expression."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    host_limit: str | None = None

    @classmethod
    def all(cls) -> FleetTarget:
        return cls()

    @classmethod
    def limited_to(cls, host_limit: str) -> FleetTarget:
        return cls(host_limit=host_limit)


class DeployRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: DeploymentMode = DeploymentMode.APPLY
    target: FleetTarget = Field(default_factory=FleetTarget.all)


class PreflightPolicy(StrEnum):
    ENFORCE = "enforce"
    OVERRIDE = "override"


class CertificateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    site: SiteName
    registration_email: str | None = None
    preflight: PreflightPolicy = PreflightPolicy.ENFORCE


class WorkflowKind(StrEnum):
    DEPLOYMENT = "deployment"
    ROLLBACK = "rollback"
    CERTIFICATE = "certificate"


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
