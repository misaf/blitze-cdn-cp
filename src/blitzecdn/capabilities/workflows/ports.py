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
    ) -> Workflow:
        """Record a new workflow of ``kind`` that ``operator`` started."""
        ...

    def advance(
        self,
        workflow_id: str,
        status: WorkflowStatus,
        *,
        step: WorkflowStep | None = None,
        error: str | None = None,
    ) -> Workflow:
        """Move a workflow to ``status``, appending ``step`` and recording ``error``.

        Raises ``NotFoundError`` for an unknown workflow.
        """
        ...

    def get(self, workflow_id: str) -> Workflow:
        """One workflow; raises ``NotFoundError`` if there is none by that id."""
        ...

    def unfinished(self) -> list[Workflow]:
        """Every workflow not yet finished, oldest first."""
        ...

    def prune_finished(self, keep: int) -> int:
        """Drop finished workflows beyond the newest ``keep``; returns how many.

        Unfinished and NEEDS_REVIEW workflows always stay: the latter is the only
        record of interrupted external work until an operator acknowledges it.
        """
        ...


class WorkflowHistory(Protocol):
    """The journal, read-only: the service writes through `WorkflowJournal`."""

    def get(self, workflow_id: str) -> Workflow:
        """One workflow; raises ``NotFoundError`` if there is none by that id."""
        ...

    def paginate_workflows(self, *, limit: int, offset: int) -> PageResult[Workflow]:
        """One page of the journal, newest first."""
        ...

    def list_workflows(self, limit: int = 100) -> list[Workflow]:
        """The ``limit`` most recent workflows, newest first."""
        ...

    def statistics(self) -> WorkflowStatistics:
        """Counts across the whole retained journal, for monitoring."""
        ...
