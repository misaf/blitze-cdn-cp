"""What the workflow service calls, and what the API reads."""

from __future__ import annotations

from typing import Protocol

from blitzecdn.capabilities.workflows.domain import (
    Workflow,
    WorkflowKind,
    WorkflowStatus,
    WorkflowStep,
)
from blitzecdn.capabilities.workflows.domain.statistics import WorkflowStatistics
from blitzecdn.core.domain.pagination import PageResult

__all__ = ["WorkflowHistory", "WorkflowJournal"]


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


class WorkflowHistory(Protocol):
    """The journal, read-only: the service writes through `WorkflowJournal`."""

    def get(self, workflow_id: str) -> Workflow: ...

    def paginate_workflows(
        self, *, limit: int, offset: int
    ) -> PageResult[Workflow]: ...

    def list_workflows(self, limit: int = 100) -> list[Workflow]: ...

    def statistics(self) -> WorkflowStatistics: ...
