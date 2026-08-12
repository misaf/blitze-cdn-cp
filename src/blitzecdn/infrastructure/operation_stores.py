"""Persistence for durable workflows and transactional outbox events."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from blitzecdn.domain.operations import (
    OutboxEvent,
    Workflow,
    WorkflowKind,
    WorkflowStatus,
    WorkflowStep,
)
from blitzecdn.exceptions import NotFoundError
from blitzecdn.infrastructure.engine import Database
from blitzecdn.infrastructure.models import OutboxEventRow, WorkflowRow


class WorkflowStore:
    """Durable progress for work spanning SQLite and external systems."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def create(
        self,
        workflow_id: str,
        kind: WorkflowKind,
        operator: str,
        resource_id: str | None = None,
    ) -> Workflow:
        now = self._db.now()
        with self._db.session() as session:
            row = WorkflowRow(
                id=workflow_id,
                kind=kind.value,
                resource_id=resource_id,
                status=WorkflowStatus.PENDING.value,
                operator=operator,
                created_at=now,
                updated_at=now,
                steps=[],
            )
            session.add(row)
            session.flush()
            return self._workflow(row)

    def advance(
        self,
        workflow_id: str,
        status: WorkflowStatus,
        *,
        step: WorkflowStep | None = None,
        error: str | None = None,
    ) -> Workflow:
        with self._db.session() as session:
            row = session.get(WorkflowRow, workflow_id)
            if row is None:
                raise NotFoundError(f"workflow {workflow_id!r} does not exist")
            if step is not None:
                # Rebound rather than appended in place: a JSON column is a
                # plain list, and SQLAlchemy only notices a mutation it can see
                # as an attribute set.
                row.steps = [*row.steps, step.model_dump(mode="json")]
            row.status = status.value
            row.error = error
            row.updated_at = self._db.now()
            session.flush()
            return self._workflow(row)

    def get(self, workflow_id: str) -> Workflow:
        with self._db.session() as session:
            row = session.get(WorkflowRow, workflow_id)
            if row is None:
                raise NotFoundError(f"workflow {workflow_id!r} does not exist")
            return self._workflow(row)

    def unfinished(self) -> list[Workflow]:
        with self._db.session() as session:
            rows = session.scalars(
                select(WorkflowRow)
                .where(
                    WorkflowRow.status.in_(
                        (WorkflowStatus.PENDING.value, WorkflowStatus.RUNNING.value)
                    )
                )
                .order_by(WorkflowRow.created_at)
            ).all()
            return [self._workflow(row) for row in rows]

    def list_workflows(self, limit: int = 100) -> list[Workflow]:
        with self._db.session() as session:
            rows = session.scalars(
                select(WorkflowRow).order_by(WorkflowRow.created_at.desc()).limit(limit)
            ).all()
            return [self._workflow(row) for row in rows]

    @staticmethod
    def _workflow(row: WorkflowRow) -> Workflow:
        return Workflow.model_validate(
            {
                "id": row.id,
                "kind": row.kind,
                "resource_id": row.resource_id,
                "status": row.status,
                "operator": row.operator,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "steps": tuple(row.steps),
                "error": row.error,
            }
        )


class OutboxStore:
    """Transactional integration events with retry metadata."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def enqueue(self, topic: str, event_key: str, payload: dict[str, Any]) -> None:
        with self._db.session() as session:
            session.execute(
                sqlite_insert(OutboxEventRow)
                .values(
                    topic=topic,
                    event_key=event_key,
                    payload=payload,
                    created_at=self._db.now(),
                )
                .on_conflict_do_nothing(index_elements=[OutboxEventRow.event_key])
            )

    def pending(self, limit: int = 100) -> list[OutboxEvent]:
        with self._db.session() as session:
            rows = session.scalars(
                select(OutboxEventRow)
                .where(OutboxEventRow.delivered_at.is_(None))
                .order_by(OutboxEventRow.id)
                .limit(limit)
            ).all()
            return [self._event(row) for row in rows]

    def delivered(self, event_id: int) -> None:
        with self._db.session() as session:
            session.execute(
                update(OutboxEventRow)
                .where(
                    OutboxEventRow.id == event_id,
                    OutboxEventRow.delivered_at.is_(None),
                )
                .values(
                    delivered_at=self._db.now(),
                    attempts=OutboxEventRow.attempts + 1,
                    last_error=None,
                )
            )

    def failed(self, event_id: int, error: str) -> None:
        with self._db.session() as session:
            session.execute(
                update(OutboxEventRow)
                .where(
                    OutboxEventRow.id == event_id,
                    OutboxEventRow.delivered_at.is_(None),
                )
                .values(attempts=OutboxEventRow.attempts + 1, last_error=error)
            )

    def undelivered(self) -> int:
        """How many events are still waiting. For operators, not for delivery."""
        with self._db.session() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(OutboxEventRow)
                    .where(OutboxEventRow.delivered_at.is_(None))
                )
                or 0
            )

    @staticmethod
    def _event(row: OutboxEventRow) -> OutboxEvent:
        return OutboxEvent.model_validate(
            {
                "id": row.id,
                "topic": row.topic,
                "event_key": row.event_key,
                "payload": row.payload,
                "created_at": row.created_at,
                "delivered_at": row.delivered_at,
                "attempts": row.attempts,
                "last_error": row.last_error,
            }
        )
