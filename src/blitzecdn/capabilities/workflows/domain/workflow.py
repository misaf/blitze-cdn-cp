"""Durable progress for one operation that crosses out of a transaction.

A record of work SQLite cannot roll back, so a restart mid-flight leaves
something to read. `WorkflowKind` is a validated shape, not an enum: each
capability names its own kind.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from blitzecdn.core.domain.identifiers import Operator

__all__ = ["Workflow", "WorkflowKind", "WorkflowStatus", "WorkflowStep"]

#: The kind of work, named by the capability doing it: lowercase, identifier
#: shaped and bounded, as `workflows_kind_check` also asserts in SQL.
WorkflowKind = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, pattern=r"^[a-z][a-z0-9_]*$", max_length=64
    ),
]


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
