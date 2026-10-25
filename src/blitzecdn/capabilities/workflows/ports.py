"""What the workflow service calls, and what a caller of it must provide.

`WorkflowJournal` is the store this capability's service writes through: a port
with a service, a store and a table behind it is a capability seen from the
outside, not a cross-cutting contract in core.
"""

from __future__ import annotations

from typing import Protocol

from blitzecdn.capabilities.workflows.domain import (
    Workflow,
    WorkflowKind,
    WorkflowStatus,
    WorkflowStep,
)

__all__ = ["WorkflowJournal"]


class WorkflowJournal(Protocol):
    def create(
        self,
        workflow_id: str,
        kind: WorkflowKind,
        operator: str,
        resource_id: str | None = None,
    ) -> Workflow: ...

    def advance(
        self,
        workflow_id: str,
        status: WorkflowStatus,
        *,
        step: WorkflowStep | None = None,
        error: str | None = None,
    ) -> Workflow: ...

    def unfinished(self) -> list[Workflow]: ...

    def prune_finished(self, keep: int) -> int: ...
