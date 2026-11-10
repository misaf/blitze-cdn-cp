"""Recording checkpoints around work SQLite cannot roll back.

When a workflow opens, what an escaping exception means on close, how many
finished ones are kept, what a restart does with unfinished ones, and how an
operator signs off on one a restart interrupted.
"""

from __future__ import annotations

import logging
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
from blitzecdn.core.domain.events import domain_event
from blitzecdn.core.exceptions import BlitzeError, ConflictError
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.ports.clock import Clock
from blitzecdn.core.ports.operations import EventRecorder

__all__ = ["WorkflowCoordinator", "WorkflowProgress"]

_LOGGER = logging.getLogger(__name__)

#: What the journal says about an exception outside the `BlitzeError` taxonomy.
_UNEXPECTED = "unexpected failure; the controller log has the details"


def recorded_error(exc: BaseException) -> str:
    """What the journal may keep about ``exc``.

    The journal is persisted, backed up and served by the API, and exception
    messages can carry infrastructure credentials. A `BlitzeError` is written to
    be shown to an operator, so its message is kept; anything else is reduced to
    its type, with the full text left to the local log.
    """
    if isinstance(exc, BlitzeError):
        return f"{type(exc).__name__}: {exc}"
    return f"{type(exc).__name__}: {_UNEXPECTED}"


class WorkflowProgress:
    def __init__(
        self,
        *,
        checkpoint: Callable[[str, dict[str, Any] | None], Workflow],
    ) -> None:
        self.checkpoint = checkpoint
        self.error: str | None = None

    def fail(self, error: str) -> None:
        """Mark the workflow failed with ``error`` once it closes, without raising."""
        self.error = error


class WorkflowCoordinator:
    """Records idempotent checkpoints around work SQLite cannot roll back."""

    def __init__(
        self,
        *,
        journal: WorkflowJournal,
        uow: UnitOfWork,
        events: EventRecorder,
        retention: int = 1000,
        clock: Clock,
    ) -> None:
        self.journal = journal
        self.events = events
        self.clock = clock
        self.uow = uow
        #: How many finished workflows to keep, pruned whenever one closes.
        self.retention = retention

    @contextmanager
    def run(
        self, kind: WorkflowKind, operator: str, resource_id: str | None = None
    ) -> Iterator[WorkflowProgress]:
        """Journal the work in the ``with`` block as one workflow of ``kind``.

        Leaving normally records success, or failure if ``fail`` was called; an
        exception records failure and propagates. Only a ``BlitzeError``'s message
        is journalled; anything else is reduced to its type.
        """
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
            if not isinstance(exc, BlitzeError):
                # The journal keeps only the type; the local log keeps the rest.
                _LOGGER.error(
                    "workflow failed unexpectedly",
                    exc_info=exc,
                    extra={
                        "workflow_id": workflow_id,
                        "workflow_kind": kind,
                        "resource_id": resource_id,
                    },
                )
            with self.uow.transaction():
                self.journal.advance(
                    workflow_id,
                    WorkflowStatus.FAILED,
                    error=recorded_error(exc),
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

    def acknowledge(self, workflow_id: str, operator: str) -> Workflow:
        """Record that an operator reviewed an interrupted workflow.

        Only NEEDS_REVIEW can be acknowledged. The workflow becomes REVIEWED,
        keeps its error and checkpoints, gains a ``reviewed`` step naming the
        operator, and from then on is pruned like any finished workflow.
        """
        with self.uow.transaction():
            current = self.journal.get(workflow_id)
            if current.status is not WorkflowStatus.NEEDS_REVIEW:
                raise ConflictError(
                    f"workflow {workflow_id!r} is {current.status.value}; only "
                    f"{WorkflowStatus.NEEDS_REVIEW.value} workflows can be "
                    "acknowledged"
                )
            reviewed = self.journal.advance(
                workflow_id,
                WorkflowStatus.REVIEWED,
                step=WorkflowStep(
                    name="reviewed",
                    completed_at=self.clock(),
                    details={"operator": operator},
                ),
                error=current.error,
            )
            self.events.record(
                domain_event(
                    operator,
                    "workflow.reviewed",
                    "workflow",
                    workflow_id,
                    {"kind": reviewed.kind, "resource_id": reviewed.resource_id},
                )
            )
            self.journal.prune_finished(self.retention)
        return reviewed

    def reconcile_interrupted(
        self, *, resource_id: str | None = None
    ) -> list[Workflow]:
        """Turn work interrupted by a restart into NEEDS_REVIEW.

        Every unfinished workflow only at startup, under the deployment lock; given a
        ``resource_id``, only that resource's, leaving work still running elsewhere.
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
        for workflow in recovered:
            _LOGGER.warning(
                "interrupted workflow requires recovery review",
                extra={
                    "workflow_id": workflow.id,
                    "workflow_kind": workflow.kind,
                    "resource_id": workflow.resource_id,
                    "last_checkpoint": workflow.steps[-1].name
                    if workflow.steps
                    else None,
                },
            )
        return recovered
