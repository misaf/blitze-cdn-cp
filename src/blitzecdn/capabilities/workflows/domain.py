"""Durable progress for one operation that crosses out of a transaction.

A workflow is the record of work SQLite cannot roll back — a fleet converging,
a CA issuing a certificate — written so a controller that restarts mid-flight
leaves an operator something to read rather than silence.

`WorkflowKind` is a *shape*, not a list. It was a closed enum naming
`deployment`, `rollback` and `certificate` — the third of which is
`blitzecdn-certificates`' concept, so this capability enumerated the work of a
distribution that may not be installed, and a wheel that wanted a journal entry
of its own had to be added to an enum in a package it does not ship. Whose work
it is belongs to whoever is doing it: each capability declares its own name
beside the operation it names, and this validates that the name is one a
journal can hold. It is the arrangement `capability_requirements` already uses
for capability tokens — declared by the contract that wants one, merged by
something that names none.

The identifier aliases that used to sit above these models are core's
vocabulary rather than this capability's, and are
:mod:`blitzecdn.core.domain.identifiers` now.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from blitzecdn.core.domain.identifiers import Operator

__all__ = ["Workflow", "WorkflowKind", "WorkflowStatus", "WorkflowStep"]

#: What kind of work a journal entry records, named by the capability doing it.
#:
#: Lowercase and identifier-shaped so it reads the same in an API response, a
#: log line and a `WHERE` clause, and bounded so a name cannot become a
#: payload. `workflows_kind_check` asserts the same three properties in SQL,
#: which is as much as a database can say about a set it does not know.
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
