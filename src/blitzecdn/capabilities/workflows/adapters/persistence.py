"""The workflow journal store, with the read side the API publishes."""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select

from blitzecdn.capabilities.workflows.adapters.tables import WorkflowRow
from blitzecdn.capabilities.workflows.domain import (
    Workflow,
    WorkflowKind,
    WorkflowStatus,
    WorkflowStep,
)
from blitzecdn.capabilities.workflows.domain.statistics import WorkflowStatistics
from blitzecdn.core.domain.pagination import PageResult
from blitzecdn.core.exceptions import NotFoundError
from blitzecdn.core.persistence.engine import Database
from blitzecdn.core.persistence.pagination import read_page


class WorkflowStore:
    """Durable progress for work spanning SQLite and external systems."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def statistics(self) -> WorkflowStatistics:
        with self._db.session() as session:
            counts = {
                WorkflowStatus(status): count
                for status, count in session.execute(
                    select(WorkflowRow.status, func.count()).group_by(
                        WorkflowRow.status
                    )
                )
            }
            count, duration = session.execute(
                select(
                    func.count(),
                    func.sum(
                        func.max(
                            0,
                            (
                                func.julianday(WorkflowRow.updated_at)
                                - func.julianday(WorkflowRow.created_at)
                            )
                            * 86400,
                        )
                    ),
                ).where(
                    WorkflowRow.status.in_(
                        [
                            WorkflowStatus.SUCCEEDED.value,
                            WorkflowStatus.FAILED.value,
                        ]
                    )
                )
            ).one()
        return WorkflowStatistics(
            counts=counts,
            completed_count=count,
            duration_seconds_sum=duration or 0,
        )

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
                kind=kind,
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
                # Rebound, not appended: SQLAlchemy only notices attribute sets on JSON.
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

    def prune_finished(self, keep: int) -> int:
        """Drop finished workflows beyond the newest ``keep``; unfinished ones never go.

        NEEDS_REVIEW counts as finished: an operator has been shown it.
        """
        with self._db.session() as session:
            unfinished = (WorkflowStatus.PENDING.value, WorkflowStatus.RUNNING.value)
            survivors = (
                select(WorkflowRow.id)
                .where(WorkflowRow.status.not_in(unfinished))
                .order_by(WorkflowRow.created_at.desc())
                .limit(keep)
                .scalar_subquery()
            )
            result = session.execute(
                delete(WorkflowRow).where(
                    WorkflowRow.status.not_in(unfinished),
                    WorkflowRow.id.not_in(survivors),
                )
            )
            return cast("CursorResult[Any]", result).rowcount

    def paginate_workflows(self, *, limit: int, offset: int) -> PageResult[Workflow]:
        with self._db.session() as session:
            return read_page(
                session,
                select(WorkflowRow).order_by(
                    WorkflowRow.created_at.desc(), WorkflowRow.id.desc()
                ),
                self._workflow,
                limit=limit,
                offset=offset,
            )

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
