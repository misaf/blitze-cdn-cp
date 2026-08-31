from __future__ import annotations

from typing import Protocol

from blitzecdn.core.audit import AuditEvent
from blitzecdn.core.events import DomainEvent
from blitzecdn.core.operations import (
    Workflow,
    WorkflowKind,
    WorkflowStatus,
    WorkflowStep,
)


class AuditTrail(Protocol):
    """The append-only operator log, as the entry layers need to read it.

    Read-only on purpose. Application services write through
    :class:`EventRecorder`; entry layers cannot manufacture audit rows for
    actions no use case performed.
    """

    def get_audit_event(self, event_id: int) -> AuditEvent: ...

    def list_audit_events(self, limit: int = 100) -> list[AuditEvent]: ...


class EventRecorder(Protocol):
    """Durably record one application event in the surrounding transaction."""

    def record(self, event: DomainEvent) -> None: ...


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


__all__ = ["AuditTrail", "EventRecorder", "WorkflowJournal"]
