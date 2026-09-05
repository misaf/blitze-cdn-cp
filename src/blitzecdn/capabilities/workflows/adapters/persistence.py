"""The journal, on the one SQLite database.

`WorkflowStore` satisfies `workflows.ports.WorkflowJournal`, and the read side
the API publishes — `get` and `list_workflows` — is on the same object because
there is one table and one owner of it.
"""

# SQLModel annotates a table attribute as its instance value (for example
# ``str``), while SQLAlchemy turns that attribute into a SQL expression when
# accessed on the class. Mypy cannot model that duality in the bulk statements
# below, which SQLModel deliberately leaves to its SQLAlchemy foundation.

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import CursorResult, delete, select
from sqlmodel import col

from blitzecdn.capabilities.workflows.adapters.tables import WorkflowRow
from blitzecdn.capabilities.workflows.domain import (
    Workflow,
    WorkflowKind,
    WorkflowStatus,
    WorkflowStep,
)
from blitzecdn.core.exceptions import NotFoundError
from blitzecdn.core.persistence.engine import Database


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
                    col(WorkflowRow.status).in_(
                        (WorkflowStatus.PENDING.value, WorkflowStatus.RUNNING.value)
                    )
                )
                .order_by(col(WorkflowRow.created_at))
            ).all()
            return [self._workflow(row) for row in rows]

    def prune_finished(self, keep: int) -> int:
        """Drop finished workflows beyond the newest ``keep``.

        Never an unfinished one. Those are what ``reconcile_interrupted`` turns
        into NEEDS_REVIEW at startup, and a workflow removed before anyone
        looked at it is the record of ambiguous external work — a certificate
        the CA may have issued — disappearing.

        NEEDS_REVIEW counts as finished for this and is therefore prunable,
        which is deliberate: it is a terminal state an operator has been shown,
        and keeping the newest ``keep`` of them is what retention means
        everywhere else here.
        """
        with self._db.session() as session:
            unfinished = (WorkflowStatus.PENDING.value, WorkflowStatus.RUNNING.value)
            survivors = (
                select(col(WorkflowRow.id))
                .where(col(WorkflowRow.status).not_in(unfinished))
                .order_by(col(WorkflowRow.created_at).desc())
                .limit(keep)
                .scalar_subquery()
            )
            result = session.execute(
                delete(WorkflowRow).where(
                    col(WorkflowRow.status).not_in(unfinished),
                    col(WorkflowRow.id).not_in(survivors),
                )
            )
            return cast("CursorResult[Any]", result).rowcount

    def list_workflows(self, limit: int = 100) -> list[Workflow]:
        with self._db.session() as session:
            rows = session.scalars(
                select(WorkflowRow)
                .order_by(col(WorkflowRow.created_at).desc())
                .limit(limit)
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
