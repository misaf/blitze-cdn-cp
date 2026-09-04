"""Persistence for deployment history and snapshots."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import CursorResult, Result, delete, select, update
from sqlmodel import col

from blitzecdn.capabilities.deployments.domain import (
    Deployment,
    DeploymentStatus,
    require_transition,
)
from blitzecdn.core.domain.runs import AnsibleRun, RunStatus
from blitzecdn.core.exceptions import ConflictError, NotFoundError
from blitzecdn.core.persistence.engine import Database
from blitzecdn.core.persistence.models import DeploymentRow


def _rows_affected(result: Result[Any]) -> int:
    """``rowcount`` for a DML statement.

    ``Session.execute`` is typed as returning ``Result``, which has no
    ``rowcount`` — only the ``CursorResult`` a DML statement actually produces
    does. The narrowing is here rather than at each call site so "how many rows
    did that touch" stays one readable expression.
    """
    return cast("CursorResult[Any]", result).rowcount


class DeploymentStore:
    """Deployment history.

    ``snapshot_source`` is injected rather than read from the other stores
    directly: a snapshot spans sites and zones, so owning it here would give
    this store a reason to touch every table it otherwise does not.
    """

    def __init__(self, database: Database, snapshot_source: Callable[[], str]) -> None:
        self._db = database
        self._snapshot_source = snapshot_source

    def snapshot(self) -> str:
        """Desired state as it stands right now, not as any deployment saw it.

        Exposed because the callers that want to *record* a snapshot and the
        callers that want to *read the current one* — validate, and choosing a
        rollback target — are the same callers, and making them hold a second
        object for the second question only invited them to be given the whole
        of persistence to get it.
        """
        return self._snapshot_source()

    def create_deployment(
        self,
        operator: str,
        *,
        check_mode: bool,
        rollback_of: str | None = None,
        snapshot: str | None = None,
        host_limit: str | None = None,
        canonical_digest: str | None = None,
    ) -> Deployment:
        deployment_id = uuid4().hex
        with self._db.session() as session:
            row = DeploymentRow(
                id=deployment_id,
                status=DeploymentStatus.QUEUED.value,
                operator=operator,
                check_mode=check_mode,
                rollback_of=rollback_of,
                created_at=self._db.now(),
                snapshot=snapshot or self._snapshot_source(),
                host_limit=host_limit,
                canonical_digest=canonical_digest,
            )
            session.add(row)
            session.flush()
            # Built from the row this call wrote, inside the transaction that
            # wrote it. Re-reading afterwards was a second transaction, so the
            # object returned described whatever the table said by then rather
            # than what was just created.
            return self._deployment(row)

    def transition(
        self,
        deployment_id: str,
        expected: DeploymentStatus,
        target: DeploymentStatus,
        **values: Any,
    ) -> Deployment:
        # The lifecycle table belongs to the domain and is enforced before the
        # write, so an illegal step is refused even when no row would match.
        # The status guard below then owns the race: a lawful step against a
        # stale expected state is a ConflictError, not a ValueError.
        require_transition(expected, target)
        allowed = {"started_at", "finished_at", "result"}
        if set(values) - allowed:
            raise ValueError("unsupported deployment transition fields")
        result = values.get("result")
        if result is not None and not isinstance(result, AnsibleRun):
            raise ValueError("deployment result must be an AnsibleRun")
        with self._db.session() as session:
            row = session.get(DeploymentRow, deployment_id)
            if row is None or row.status != expected.value:
                raise ConflictError(f"deployment {deployment_id} is not {expected}")
            row.status = target.value
            # Each field is left alone when the caller did not supply it —
            # what COALESCE did in the SQL this replaced. A transition that
            # only finishes a run must not blank the time it started.
            for field in ("started_at", "finished_at"):
                supplied = values.get(field)
                if supplied is not None:
                    setattr(row, field, supplied)
            if result is not None:
                row.result = result.model_dump(mode="json")
            # Inside the transaction, for the same reason as above: a caller
            # that just moved a deployment to RUNNING must be handed the
            # deployment it moved, not the state of the row a moment later.
            return self._deployment(row)

    def get_deployment(self, deployment_id: str) -> Deployment:
        with self._db.session() as session:
            row = session.get(DeploymentRow, deployment_id)
            if row is None:
                raise NotFoundError(f"deployment {deployment_id!r} does not exist")
            return self._deployment(row)

    def deployment_snapshot(self, deployment_id: str) -> str:
        with self._db.session() as session:
            row = session.get(DeploymentRow, deployment_id)
            if row is None:
                raise NotFoundError(f"deployment {deployment_id!r} does not exist")
            return row.snapshot

    def list_deployments(self, limit: int = 20) -> list[Deployment]:
        with self._db.session() as session:
            rows = session.scalars(
                select(DeploymentRow)
                .order_by(col(DeploymentRow.created_at).desc())
                .limit(limit)
            ).all()
            return [self._deployment(row) for row in rows]

    def queued_deployments(self) -> list[Deployment]:
        with self._db.session() as session:
            rows = session.scalars(
                select(DeploymentRow)
                .where(col(DeploymentRow.status) == DeploymentStatus.QUEUED.value)
                .order_by(col(DeploymentRow.created_at))
            ).all()
            return [self._deployment(row) for row in rows]

    def abandon_running(self) -> int:
        """Close out deployments the last controller process left in flight.

        They are given a result of their own rather than only a status: every
        reader now expects to find why a deployment ended in `result`, and
        "the controller restarted" is as much an answer as a failed task is.
        """
        now = self._db.now()
        abandoned = AnsibleRun(
            id=uuid4().hex,
            playbook="",
            status=RunStatus.UNSTARTED,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            error="the controller restarted before this deployment completed",
        ).model_dump(mode="json")
        with self._db.session() as session:
            return _rows_affected(
                session.execute(
                    update(DeploymentRow)
                    .where(col(DeploymentRow.status) == DeploymentStatus.RUNNING.value)
                    .values(
                        status=DeploymentStatus.ABANDONED.value,
                        finished_at=now,
                        result=abandoned,
                    )
                )
            )

    def prune_history(self, keep: int) -> int:
        """Drop the oldest check-mode deployments beyond the newest ``keep``.

        Only check-mode rows. A real deployment is history an operator reads
        after an incident and, more to the point, is what
        :meth:`successful_rollback_target` chooses from — pruning one could
        remove the snapshot the fleet needs to go back to. Check-mode runs can
        never be that: rollback selection filters them out.

        They are also the ones that accumulate. The drift timer fires hourly
        and each firing writes a row carrying a *complete* copy of every zone
        and record, so this table grows by a full desired state every hour
        whether or not anything changed. Run-log retention exists for the same
        reason on the same schedule; this is that policy applied to the rows.
        """
        with self._db.session() as session:
            survivors = (
                select(col(DeploymentRow.id))
                .where(col(DeploymentRow.check_mode).is_(True))
                .order_by(col(DeploymentRow.created_at).desc())
                .limit(keep)
                .scalar_subquery()
            )
            return _rows_affected(
                session.execute(
                    delete(DeploymentRow).where(
                        col(DeploymentRow.check_mode).is_(True),
                        col(DeploymentRow.id).not_in(survivors),
                    )
                )
            )

    def successful_rollback_target(self, current_snapshot: str) -> Deployment:
        # `host_limit IS NULL` keeps canaries out of the automatic choice. A
        # limited run only proves one edge reached that snapshot, so rolling
        # the fleet back to it would converge most edges onto a state they
        # were never running. An operator can still name one explicitly.
        with self._db.session() as session:
            row = session.scalars(
                select(DeploymentRow)
                .where(
                    col(DeploymentRow.status) == DeploymentStatus.SUCCEEDED.value,
                    col(DeploymentRow.check_mode).is_(False),
                    col(DeploymentRow.snapshot) != current_snapshot,
                    col(DeploymentRow.host_limit).is_(None),
                )
                .order_by(col(DeploymentRow.created_at).desc())
                .limit(1)
            ).first()
            if row is None:
                raise NotFoundError("no different successful deployment is available")
            return self._deployment(row)

    @staticmethod
    def _deployment(row: DeploymentRow) -> Deployment:
        return Deployment.model_validate(
            {
                "id": row.id,
                "status": row.status,
                "operator": row.operator,
                "check_mode": row.check_mode,
                "host_limit": row.host_limit,
                "rollback_of": row.rollback_of,
                "canonical_digest": row.canonical_digest,
                "created_at": row.created_at,
                "started_at": row.started_at,
                "finished_at": row.finished_at,
                "result": row.result,
            }
        )


__all__ = ["DeploymentStore"]
