"""Durable workflow progress and recovery for external work."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from blitzecdn.application.ports.common import UnitOfWork
from blitzecdn.application.ports.operations import WorkflowJournal
from blitzecdn.domain.operations import (
    Workflow,
    WorkflowKind,
    WorkflowStatus,
    WorkflowStep,
)


class WorkflowProgress:
    def __init__(
        self,
        *,
        checkpoint: Callable[[str, dict[str, Any] | None], Workflow],
    ) -> None:
        self.checkpoint = checkpoint
        self.error: str | None = None

    def fail(self, error: str) -> None:
        self.error = error


class WorkflowCoordinator:
    """Records idempotent checkpoints around work SQLite cannot roll back."""

    def __init__(
        self,
        *,
        journal: WorkflowJournal,
        uow: UnitOfWork,
        retention: int = 1000,
    ) -> None:
        self.journal = journal
        self.uow = uow
        #: How many finished workflows to keep. Applied when one closes, so the
        #: policy runs whenever the thing it bounds happens rather than needing
        #: a timer that may never have been installed.
        self.retention = retention

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
            progress = WorkflowProgress(checkpoint=checkpoint)
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
                self.journal.prune_finished(self.retention)

    def reconcile_interrupted(self) -> list[Workflow]:
        """Turn work interrupted by a restart into operator-visible state."""
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
