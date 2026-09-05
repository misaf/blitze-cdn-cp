"""Durable progress for one operation that crosses out of a transaction.

A workflow is the record of work SQLite cannot roll back — a fleet converging,
a CA issuing a certificate — written so a controller that restarts mid-flight
leaves an operator something to read rather than silence.

`WorkflowKind` is closed, and it is checked in the database as well
(`workflows_kind_check`), so adding a kind is a migration rather than an edit
here.

The identifier aliases that used to sit above these models are core's
vocabulary rather than this capability's, and are
:mod:`blitzecdn.core.domain.identifiers` now.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from blitzecdn.core.domain.identifiers import Operator

__all__ = ["Workflow", "WorkflowKind", "WorkflowStatus", "WorkflowStep"]


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
