"""Persistence for deployment history, the releases it names, and requirements."""

from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import CursorResult, Result, delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from blitzecdn.capabilities.deployments.adapters.tables import (
    DeploymentRequirementRow,
    DeploymentRow,
    DeploymentTargetRow,
)
from blitzecdn.capabilities.deployments.domain import (
    TERMINAL_STATUSES,
    Deployment,
    DeploymentRequirementKind,
    DeploymentStatus,
    DeploymentTarget,
    TargetPhase,
    TargetStatus,
    abandoned_run,
)
from blitzecdn.core.domain.pagination import PageResult
from blitzecdn.core.exceptions import ConflictError, NotFoundError
from blitzecdn.core.persistence.engine import Database
from blitzecdn.core.persistence.pagination import read_page


def _rows_affected(result: Result[Any]) -> int:
    """``rowcount`` for a DML statement, narrowed from ``Result`` once, here."""
    return cast("CursorResult[Any]", result).rowcount


class DeploymentStore:
    """Deployment history, and which release each run converged, by reference."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def in_use(self) -> Sequence[str]:
        """Every release some deployment still names; pruning must keep them."""
        with self._db.session() as session:
            return list(
                session.scalars(select(DeploymentRow.release_id).distinct()).all()
            )

    def create_deployment(
        self,
        operator: str,
        *,
        release_id: str,
        check_mode: bool,
        rollback_of: str | None = None,
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
                release_id=release_id,
                host_limit=host_limit,
                canonical_digest=canonical_digest,
            )
            session.add(row)
            session.flush()
            # Built from the row just written, inside the same transaction.
            return self._deployment(row)

    def save(self, before: Deployment, after: Deployment) -> Deployment:
        """Record a transition, only while the row is still in ``before``'s status.

        The domain method decided the step is lawful; this decides the race, so the
        loser of two processes gets a ``ConflictError`` instead of overwriting.
        """
        if after.id != before.id:
            raise ValueError("a transition cannot change which deployment it is")
        with self._db.session() as session:
            row = session.get(DeploymentRow, before.id)
            if row is None or row.status != before.status.value:
                raise ConflictError(f"deployment {before.id} is not {before.status}")
            row.status = after.status.value
            row.started_at = after.started_at
            row.finished_at = after.finished_at
            row.result = after.result.model_dump(mode="json") if after.result else None
            row.error = after.error
            # Inside the transaction: the caller is handed the deployment it
            # moved, not the state of the row a moment later.
            return self._deployment(row)

    def get_deployment(self, deployment_id: str) -> Deployment:
        with self._db.session() as session:
            row = session.get(DeploymentRow, deployment_id)
            if row is None:
                raise NotFoundError(f"deployment {deployment_id!r} does not exist")
            return self._deployment(row)

    def deployment_release(self, deployment_id: str) -> str:
        """Which release this deployment converged, or is about to."""
        with self._db.session() as session:
            row = session.get(DeploymentRow, deployment_id)
            if row is None:
                raise NotFoundError(f"deployment {deployment_id!r} does not exist")
            return row.release_id

    def paginate_deployments(
        self, *, limit: int, offset: int
    ) -> PageResult[Deployment]:
        with self._db.session() as session:
            return read_page(
                session,
                select(DeploymentRow).order_by(
                    DeploymentRow.created_at.desc(), DeploymentRow.id.desc()
                ),
                self._deployment,
                limit=limit,
                offset=offset,
            )

    def list_deployments(self, limit: int = 20) -> list[Deployment]:
        with self._db.session() as session:
            rows = session.scalars(
                select(DeploymentRow)
                .order_by(DeploymentRow.created_at.desc())
                .limit(limit)
            ).all()
            return [self._deployment(row) for row in rows]

    def latest_successful_applied(self) -> Deployment | None:
        """The newest successful applied run, regardless of newer checks."""
        with self._db.session() as session:
            row = session.scalars(
                select(DeploymentRow)
                .where(
                    DeploymentRow.status == DeploymentStatus.SUCCEEDED.value,
                    DeploymentRow.check_mode.is_(False),
                )
                .order_by(DeploymentRow.created_at.desc())
                .limit(1)
            ).first()
            return self._deployment(row) if row is not None else None

    def queued_deployments(self) -> list[Deployment]:
        with self._db.session() as session:
            rows = session.scalars(
                select(DeploymentRow)
                .where(DeploymentRow.status == DeploymentStatus.QUEUED.value)
                .order_by(DeploymentRow.created_at)
            ).all()
            return [self._deployment(row) for row in rows]

    def running_deployments(self) -> list[Deployment]:
        with self._db.session() as session:
            rows = session.scalars(
                select(DeploymentRow)
                .where(DeploymentRow.status == DeploymentStatus.RUNNING.value)
                .order_by(DeploymentRow.created_at)
            ).all()
            return [self._deployment(row) for row in rows]

    def abandon_running(self) -> int:
        """Close out deployments a dead controller process left running."""
        now = self._db.now()
        abandoned = abandoned_run(
            "the controller restarted before this deployment completed", now=now
        ).model_dump(mode="json")
        with self._db.session() as session:
            return _rows_affected(
                session.execute(
                    update(DeploymentRow)
                    .where(DeploymentRow.status == DeploymentStatus.RUNNING.value)
                    .values(
                        status=DeploymentStatus.ABANDONED.value,
                        finished_at=now,
                        result=abandoned,
                    )
                )
            )

    def prune_history(self, keep: int) -> int:
        """Drop finished check-mode deployments beyond the newest ``keep``.

        Only check-mode rows: real deployments are what rollback chooses from, and
        queued or running checks are recovery state. The hourly drift check is what
        makes these accumulate.
        """
        finished = tuple(status.value for status in TERMINAL_STATUSES)
        with self._db.session() as session:
            survivors = (
                select(DeploymentRow.id)
                .where(
                    DeploymentRow.check_mode.is_(True),
                    DeploymentRow.status.in_(finished),
                )
                .order_by(DeploymentRow.created_at.desc())
                .limit(keep)
                .scalar_subquery()
            )
            return _rows_affected(
                session.execute(
                    delete(DeploymentRow).where(
                        DeploymentRow.check_mode.is_(True),
                        DeploymentRow.status.in_(finished),
                        DeploymentRow.id.not_in(survivors),
                    )
                )
            )

    def successful_rollback_target(self, current_release: str) -> Deployment:
        # `host_limit IS NULL` keeps canaries out of the automatic choice: one edge
        # reaching a release does not make it a state the fleet was running.
        with self._db.session() as session:
            row = session.scalars(
                select(DeploymentRow)
                .where(
                    DeploymentRow.status == DeploymentStatus.SUCCEEDED.value,
                    DeploymentRow.check_mode.is_(False),
                    DeploymentRow.release_id != current_release,
                    DeploymentRow.host_limit.is_(None),
                )
                .order_by(DeploymentRow.created_at.desc())
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
                "release_id": row.release_id,
                "host_limit": row.host_limit,
                "rollback_of": row.rollback_of,
                "canonical_digest": row.canonical_digest,
                "created_at": row.created_at,
                "started_at": row.started_at,
                "finished_at": row.finished_at,
                "result": row.result,
                "error": row.error,
            }
        )


class DeploymentRequirementStore:
    def __init__(self, database: Database) -> None:
        self._db = database

    def require(self, kind: DeploymentRequirementKind) -> None:
        with self._db.session() as session:
            session.execute(
                sqlite_insert(DeploymentRequirementRow)
                .values(kind=kind.value, requested_at=self._db.now())
                .on_conflict_do_update(
                    index_elements=[DeploymentRequirementRow.kind],
                    set_={"requested_at": self._db.now()},
                )
            )

    def clear(self, kind: DeploymentRequirementKind, *, through: datetime) -> None:
        # Conditional, so a requirement raised after ``through`` survives.
        with self._db.session() as session:
            session.execute(
                delete(DeploymentRequirementRow).where(
                    DeploymentRequirementRow.kind == kind.value,
                    DeploymentRequirementRow.requested_at <= through,
                )
            )

    def pending(self, kind: DeploymentRequirementKind) -> bool:
        return self.requested_at(kind) is not None

    def requested_at(self, kind: DeploymentRequirementKind) -> datetime | None:
        with self._db.session() as session:
            row = session.get(DeploymentRequirementRow, kind.value)
            return None if row is None else row.requested_at


__all__ = [
    "DeploymentRequirementStore",
    "DeploymentStore",
    "DeploymentTargetStore",
]


class DeploymentTargetStore:
    """Per-edge rollout progress, written under a fence.

    Every write names the generation it read, so a worker that outlived its lease
    updates nothing and is told so.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    def claim_generation(self, deployment_id: str) -> int:
        """Take this deployment over atomically, returning the new fence."""
        with self._db.session() as session:
            generation = session.execute(
                update(DeploymentRow)
                .where(DeploymentRow.id == deployment_id)
                .values(generation=DeploymentRow.generation + 1)
                .returning(DeploymentRow.generation)
            ).scalar_one_or_none()
            if generation is None:
                raise NotFoundError(f"deployment {deployment_id!r} does not exist")
            # Claim and invalidate every older target token in one commit.
            # A second `plan` call must not be needed to make takeover real.
            session.execute(
                update(DeploymentTargetRow)
                .where(DeploymentTargetRow.deployment_id == deployment_id)
                .values(fence=generation)
            )
            return int(generation)

    def generation(self, deployment_id: str) -> int:
        with self._db.session() as session:
            row = session.get(DeploymentRow, deployment_id)
            if row is None:
                raise NotFoundError(f"deployment {deployment_id!r} does not exist")
            return row.generation

    def plan(self, deployment_id: str, edges: Sequence[str], *, fence: int) -> None:
        """Record the edges this rollout aims at, and stamp every row with its fence.

        Missing rows are inserted, so a resumed rollout picks up newly added edges and
        keeps the rest's progress. Stamping each row is what lets the previous owner's
        next conditional write match nothing. Written up front so edges never attempted
        are a fact on a row.
        """
        with self._db.transaction(), self._db.session() as session:
            deployment = session.get(DeploymentRow, deployment_id)
            if deployment is None:
                raise NotFoundError(f"deployment {deployment_id!r} does not exist")
            if deployment.generation != fence:
                raise ConflictError("deployment plan belongs to a stale generation")
            existing = {
                row.edge
                for row in session.scalars(
                    select(DeploymentTargetRow).where(
                        DeploymentTargetRow.deployment_id == deployment_id
                    )
                ).all()
            }
            for edge in edges:
                if edge in existing:
                    continue
                session.add(
                    DeploymentTargetRow(
                        deployment_id=deployment_id,
                        edge=edge,
                        status=TargetStatus.PENDING.value,
                        attempts=0,
                        fence=fence,
                    )
                )
            session.flush()
            session.execute(
                update(DeploymentTargetRow)
                .where(DeploymentTargetRow.deployment_id == deployment_id)
                .values(fence=fence)
            )

    def paginate_targets(
        self, deployment_id: str, *, limit: int, offset: int
    ) -> PageResult[DeploymentTarget]:
        with self._db.session() as session:
            return read_page(
                session,
                select(DeploymentTargetRow)
                .where(DeploymentTargetRow.deployment_id == deployment_id)
                .order_by(DeploymentTargetRow.edge),
                _target,
                limit=limit,
                offset=offset,
            )

    def targets(self, deployment_id: str) -> list[DeploymentTarget]:
        with self._db.session() as session:
            rows = session.scalars(
                select(DeploymentTargetRow)
                .where(DeploymentTargetRow.deployment_id == deployment_id)
                .order_by(DeploymentTargetRow.edge)
            ).all()
            return [_target(row) for row in rows]

    def begin(
        self, deployment_id: str, edge: str, *, fence: int, now: datetime
    ) -> bool:
        return self._advance(
            deployment_id,
            edge,
            fence=fence,
            values={
                "status": TargetStatus.RUNNING.value,
                "started_at": now,
                "attempts": DeploymentTargetRow.attempts + 1,
            },
        )

    def record_phase(
        self, deployment_id: str, edge: str, phase: TargetPhase, *, fence: int
    ) -> bool:
        """Mark one phase completed for one edge."""
        return self._advance(
            deployment_id, edge, fence=fence, values={"phase": phase.value}
        )

    def finish(
        self,
        deployment_id: str,
        edge: str,
        *,
        fence: int,
        status: TargetStatus,
        now: datetime,
        error: str | None = None,
    ) -> bool:
        return self._advance(
            deployment_id,
            edge,
            fence=fence,
            values={
                "status": status.value,
                "finished_at": now,
                "last_error": error,
            },
        )

    def skip_remaining(
        self, deployment_id: str, *, fence: int, now: datetime, reason: str
    ) -> int:
        """Mark edges a stopped rollout never reached SKIPPED: none is known bad."""
        with self._db.session() as session:
            return _rows_affected(
                session.execute(
                    update(DeploymentTargetRow)
                    .where(
                        DeploymentTargetRow.deployment_id == deployment_id,
                        DeploymentTargetRow.status == TargetStatus.PENDING.value,
                        DeploymentTargetRow.fence == fence,
                    )
                    .values(
                        status=TargetStatus.SKIPPED.value,
                        finished_at=now,
                        last_error=reason,
                    )
                )
            )

    def _advance(
        self,
        deployment_id: str,
        edge: str,
        *,
        fence: int,
        values: dict[str, Any],
    ) -> bool:
        with self._db.session() as session:
            return bool(
                _rows_affected(
                    session.execute(
                        update(DeploymentTargetRow)
                        .where(
                            DeploymentTargetRow.deployment_id == deployment_id,
                            DeploymentTargetRow.edge == edge,
                            DeploymentTargetRow.fence == fence,
                        )
                        .values(**values)
                    )
                )
            )


def _target(row: DeploymentTargetRow) -> DeploymentTarget:
    return DeploymentTarget.model_validate(
        {
            "deployment_id": row.deployment_id,
            "edge": row.edge,
            "status": row.status,
            "phase": row.phase,
            "attempts": row.attempts,
            "fence": row.fence,
            "started_at": row.started_at,
            "finished_at": row.finished_at,
            "last_error": row.last_error,
        }
    )
