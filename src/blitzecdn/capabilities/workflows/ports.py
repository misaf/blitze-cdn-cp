"""What the workflow service calls, and what a caller of it must provide.

`WorkflowJournal` was `core.ports.operations.WorkflowJournal`, beside the audit
trail and the playbook runner — three ports for work that leaves the process,
one of which had a service, a store and a table behind it and so was not a
cross-cutting port at all but a capability seen from the outside.
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
