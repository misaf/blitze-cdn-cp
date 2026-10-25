"""Recording checkpoints around work SQLite cannot roll back.

The capability's decisions: when a workflow opens, what closing one means for
an exception that escaped, how many finished ones are kept, and what a
controller that restarted mid-flight does with whatever it finds unfinished.

A service over a store over a table, so a capability; the two capabilities that
keep a journal reach it through a port.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

from blitzecdn.capabilities.workflows.domain import (
    Workflow,
    WorkflowKind,
    WorkflowStatus,
    WorkflowStep,
)
from blitzecdn.capabilities.workflows.ports import WorkflowJournal
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.ports.clock import Clock, system_clock

__all__ = ["WorkflowCoordinator", "WorkflowProgress"]


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
        clock: Clock = system_clock,
    ) -> None:
        self.journal = journal
        self.clock = clock
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
                        completed_at=self.clock(),
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

    def reconcile_interrupted(
        self, *, resource_id: str | None = None
    ) -> list[Workflow]:
        """Turn work interrupted by a restart into operator-visible state.

        Every unfinished workflow by default, which is right only at startup,
        under the deployment lock, when nothing else can be mid-run. Given a
        ``resource_id``, only that resource's: the worker abandoning one
        deployment its predecessor died in must close that deployment's entry
        without touching a certificate workflow the API is running right now.
        """
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
                if resource_id is None or workflow.resource_id == resource_id
            ]
        return recovered
