"""Durable workflow progress, recovery, and post-commit event delivery."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from blitzecdn.domain.operations import (
    OutboxEvent,
    Workflow,
    WorkflowKind,
    WorkflowStatus,
    WorkflowStep,
)
from blitzecdn.ports import Outbox, UnitOfWork, WorkflowJournal

IntegrationHandler = Callable[[OutboxEvent], None]


class WorkflowProgress:
    def __init__(
        self,
        checkpoint: Callable[[str, dict[str, Any] | None], Workflow],
    ) -> None:
        self.checkpoint = checkpoint
        self.error: str | None = None

    def fail(self, error: str) -> None:
        self.error = error


class WorkflowCoordinator:
    """Records idempotent checkpoints around work SQLite cannot roll back."""

    def __init__(self, journal: WorkflowJournal, uow: UnitOfWork) -> None:
        self.journal = journal
        self.uow = uow

    @contextmanager
    def run(
        self, kind: WorkflowKind, operator: str, resource_id: str | None = None
    ) -> Iterator[WorkflowProgress]:
        workflow_id = uuid4().hex
        with self.uow.transaction():
            self.journal.create(workflow_id, kind, operator, resource_id)
            self.journal.advance(workflow_id, WorkflowStatus.RUNNING)

        def checkpoint(name: str, details: dict[str, Any] | None = None) -> Workflow:
            with self.uow.transaction():
                return self.journal.advance(
                    workflow_id,
                    WorkflowStatus.RUNNING,
                    step=WorkflowStep(
                        name=name,
                        completed_at=datetime.now(UTC),
                        details=details or {},
                    ),
                )

        try:
            progress = WorkflowProgress(checkpoint)
            yield progress
        except BaseException as exc:
            with self.uow.transaction():
                self.journal.advance(
                    workflow_id,
                    WorkflowStatus.FAILED,
                    error=f"{type(exc).__name__}: {exc}",
                )
            raise
        else:
            with self.uow.transaction():
                self.journal.advance(
                    workflow_id,
                    (
                        WorkflowStatus.FAILED
                        if progress.error
                        else WorkflowStatus.SUCCEEDED
                    ),
                    error=progress.error,
                )


class RecoveryService:
    """Turns ambiguous work from a previous process into operator-visible state."""

    def __init__(self, journal: WorkflowJournal, uow: UnitOfWork) -> None:
        self.journal = journal
        self.uow = uow

    def reconcile_interrupted(self) -> list[Workflow]:
        with self.uow.transaction():
            recovered = [
                self.journal.advance(
                    workflow.id,
                    WorkflowStatus.NEEDS_REVIEW,
                    error=(
                        "the controller restarted during external work; verify "
                        "the recorded checkpoints before retrying"
                    ),
                )
                for workflow in self.journal.unfinished()
            ]
        return recovered


#: Delivered events kept as receipts before the oldest are dropped.
OUTBOX_RETENTION = 1000


class OutboxDispatcher:
    """Retryable, idempotency-keyed delivery outside business transactions.

    ``enabled`` is false when no handler is configured, which is the ordinary
    case: nothing subscribes to these events unless an operator has wired an
    integration. The composition root asks before it subscribes the observer
    that writes them, so an unconfigured controller enqueues nothing rather
    than filling a table whose only reader is a no-op.
    """

    def __init__(self, outbox: Outbox, handlers: Mapping[str, IntegrationHandler]):
        self.outbox = outbox
        self.handlers = handlers

    @property
    def enabled(self) -> bool:
        return bool(self.handlers)

    def dispatch(self, limit: int = 100) -> int:
        """Deliver what is pending, then drop the oldest receipts.

        Pruning happens here rather than on a schedule of its own for the same
        reason run-log retention lives in the runner: a policy applied by
        whatever already runs is a policy that cannot silently stop being
        applied because a timer was never installed.
        """
        delivered = 0
        for event in self.outbox.pending(limit):
            handler = self.handlers.get(event.topic)
            if handler is None:
                continue
            try:
                handler(event)
            except Exception as exc:  # noqa: BLE001 - isolation is the contract
                self.outbox.failed(event.id, f"{type(exc).__name__}: {exc}")
                continue
            self.outbox.delivered(event.id)
            delivered += 1
        self.outbox.prune_delivered(OUTBOX_RETENTION)
        return delivered
