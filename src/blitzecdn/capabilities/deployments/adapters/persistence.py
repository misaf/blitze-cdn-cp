"""Persistence for deployment history, the releases it names, and requirements."""

from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import CursorResult, Result, delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from blitzecdn.capabilities.deployments.adapters.tables import (
    DeploymentOutcomeRow,
    DeploymentRequirementRow,
    DeploymentRow,
    DeploymentTargetRow,
)
from blitzecdn.capabilities.deployments.domain import (
    TERMINAL_STATUSES,
    ConvergenceHealth,
    ConvergenceKind,
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
        """Every release some deployment still names; pruning must keep them.

        That includes the release each was queued over, so its diff stays readable.
        """
        with self._db.reader() as session:
            converged = set(session.scalars(select(DeploymentRow.release_id)))
            previous = set(
                session.scalars(
                    select(DeploymentRow.previous_release_id).where(
                        DeploymentRow.previous_release_id.is_not(None)
                    )
                )
            )
            return sorted(converged | {name for name in previous if name})

    def create_deployment(
        self,
        operator: str,
        *,
        release_id: str,
        check_mode: bool,
        rollback_of: str | None = None,
        host_limit: str | None = None,
        canonical_digest: str | None = None,
        previous_release_id: str | None = None,
    ) -> Deployment:
        deployment_id = uuid4().hex
        with self._db.writer() as session:
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
                previous_release_id=previous_release_id,
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
        with self._db.writer() as session:
            # One conditional UPDATE, never a read then a write. The status
            # check and the write cannot be split by another process's commit,
            # and a transaction whose first statement writes waits out a busy
            # writer; a read first would pin a snapshot that the upgrade to a
            # writer then fails on at once, as "database is locked".
            row = session.execute(
                update(DeploymentRow)
                .where(
                    DeploymentRow.id == before.id,
                    DeploymentRow.status == before.status.value,
                )
                .values(
                    status=after.status.value,
                    started_at=after.started_at,
                    finished_at=after.finished_at,
                    result=(
                        after.result.model_dump(mode="json") if after.result else None
                    ),
                    error=after.error,
                )
                .returning(DeploymentRow)
            ).scalar_one_or_none()
            if row is None:
                raise ConflictError(f"deployment {before.id} is not {before.status}")
            # Inside the transaction: the caller is handed the deployment it
            # moved, not the state of the row a moment later.
            return self._deployment(row)

    def get_deployment(self, deployment_id: str) -> Deployment:
        with self._db.reader() as session:
            row = session.get(DeploymentRow, deployment_id)
            if row is None:
                raise NotFoundError(f"deployment {deployment_id!r} does not exist")
            return self._deployment(row)

    def deployment_release(self, deployment_id: str) -> str:
        """Which release this deployment converged, or is about to."""
        with self._db.reader() as session:
            row = session.get(DeploymentRow, deployment_id)
            if row is None:
                raise NotFoundError(f"deployment {deployment_id!r} does not exist")
            return row.release_id

    def paginate_deployments(
        self, *, limit: int, offset: int
    ) -> PageResult[Deployment]:
        with self._db.reader() as session:
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
        with self._db.reader() as session:
            rows = session.scalars(
                select(DeploymentRow)
                .order_by(DeploymentRow.created_at.desc())
                .limit(limit)
            ).all()
            return [self._deployment(row) for row in rows]

    def latest_successful_applied(self) -> Deployment | None:
        """The newest successful applied run, regardless of newer checks."""
        with self._db.reader() as session:
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
        with self._db.reader() as session:
            rows = session.scalars(
                select(DeploymentRow)
                .where(DeploymentRow.status == DeploymentStatus.QUEUED.value)
                .order_by(DeploymentRow.created_at)
            ).all()
            return [self._deployment(row) for row in rows]

    def running_deployments(self) -> list[Deployment]:
        with self._db.reader() as session:
            rows = session.scalars(
                select(DeploymentRow)
                .where(DeploymentRow.status == DeploymentStatus.RUNNING.value)
                .order_by(DeploymentRow.created_at)
            ).all()
            return [self._deployment(row) for row in rows]

    def abandon(self, deployment_id: str, reason: str) -> Deployment | None:
        """Finish one unfinished deployment as abandoned; ``None`` if it was not.

        Takes the rollout over as a claim does, so a worker that only paused past
        its lease is fenced out at its next target write instead of carrying the
        rollout on under a deployment already reported finished.
        """
        now = self._db.now()
        with self._db.writer() as session:
            row = session.execute(
                update(DeploymentRow)
                .where(
                    DeploymentRow.id == deployment_id,
                    DeploymentRow.status.in_(
                        (
                            DeploymentStatus.QUEUED.value,
                            DeploymentStatus.RUNNING.value,
                        )
                    ),
                )
                .values(
                    status=DeploymentStatus.ABANDONED.value,
                    finished_at=now,
                    result=abandoned_run(reason, now=now).model_dump(mode="json"),
                    error=reason,
                    generation=DeploymentRow.generation + 1,
                )
                .returning(DeploymentRow)
            ).scalar_one_or_none()
            if row is None:
                return None
            session.execute(
                update(DeploymentTargetRow)
                .where(DeploymentTargetRow.deployment_id == deployment_id)
                .values(fence=row.generation)
            )
            return self._deployment(row)

    def abandon_running(self) -> int:
        """Close out deployments a dead controller process left running."""
        now = self._db.now()
        abandoned = abandoned_run(
            "the controller restarted before this deployment completed", now=now
        ).model_dump(mode="json")
        with self._db.writer() as session:
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

    def prune_history(self, keep: int, *, keep_applied: int) -> int:
        """Drop finished deployments beyond what history and rollback still need.

        Finished check-mode runs beyond the newest ``keep``, which the hourly drift
        check makes accumulate. Finished real runs beyond the newest
        ``keep_applied``, except the newest successful full-fleet run of each of
        the ``keep_applied`` releases most recently converged that way, which is
        what rollback chooses from, and the newest successful applied run, which
        status reports. Queued and running rows are recovery state and always
        stay, as does any run a surviving rollback names. A pruned run's release,
        and the configuration bundle stored with it, is then left to release
        retention.
        """
        finished = tuple(status.value for status in TERMINAL_STATUSES)
        newest_first = (DeploymentRow.created_at.desc(), DeploymentRow.id.desc())
        succeeded = DeploymentStatus.SUCCEEDED.value
        recent_checks = (
            select(DeploymentRow.id)
            .where(DeploymentRow.check_mode.is_(True))
            .where(DeploymentRow.status.in_(finished))
            .order_by(*newest_first)
            .limit(keep)
        )
        recent_applied = (
            select(DeploymentRow.id)
            .where(DeploymentRow.check_mode.is_(False))
            # Unfinished runs survive anyway; counting them would let a backlog
            # of queued work push finished history out early.
            .where(DeploymentRow.status.in_(finished))
            .order_by(*newest_first)
            .limit(keep_applied)
        )
        latest_applied = (
            select(DeploymentRow.id)
            .where(
                DeploymentRow.check_mode.is_(False),
                DeploymentRow.status == succeeded,
            )
            .order_by(*newest_first)
            .limit(1)
        )
        # One row per release: its newest successful full-fleet run.
        ranked = (
            select(
                DeploymentRow.id,
                DeploymentRow.created_at,
                func.row_number()
                .over(partition_by=DeploymentRow.release_id, order_by=newest_first)
                .label("rank"),
            )
            .where(
                DeploymentRow.check_mode.is_(False),
                DeploymentRow.status == succeeded,
                DeploymentRow.host_limit.is_(None),
            )
            .subquery()
        )
        rollback_targets = (
            select(ranked.c.id)
            .where(ranked.c.rank == 1)
            .order_by(ranked.c.created_at.desc(), ranked.c.id.desc())
            .limit(keep_applied)
        )
        unfinished = select(DeploymentRow.id).where(
            DeploymentRow.status.not_in(finished)
        )
        rollbacks = select(DeploymentRow.id, DeploymentRow.rollback_of).where(
            DeploymentRow.rollback_of.is_not(None)
        )
        with self._db.writer() as session:
            survivors = {
                *session.scalars(recent_checks),
                *session.scalars(recent_applied),
                *session.scalars(latest_applied),
                *session.scalars(rollback_targets),
                *session.scalars(unfinished),
            }
            # A surviving rollback keeps the run it rolled back from, for the
            # foreign key; rollbacks are rare, so the chain is read whole.
            rolled_back = {
                row.id: row.rollback_of for row in session.execute(rollbacks)
            }
            pending = list(survivors)
            while pending:
                named = rolled_back.get(pending.pop())
                if named is not None and named not in survivors:
                    survivors.add(named)
                    pending.append(named)
            return _rows_affected(
                session.execute(
                    delete(DeploymentRow).where(
                        DeploymentRow.status.in_(finished),
                        DeploymentRow.id.not_in(survivors),
                    )
                )
            )

    def live_release(self) -> str | None:
        # A rollout stops at the first failing edge and withdraws only that one, so
        # a failed run can still have moved earlier edges to its release.
        reached_an_edge = (
            select(DeploymentTargetRow.deployment_id)
            .where(
                DeploymentTargetRow.deployment_id == DeploymentRow.id,
                DeploymentTargetRow.status == TargetStatus.SUCCEEDED.value,
            )
            .exists()
        )
        with self._db.reader() as session:
            return session.scalars(
                select(DeploymentRow.release_id)
                .where(
                    DeploymentRow.check_mode.is_(False),
                    (DeploymentRow.status == DeploymentStatus.SUCCEEDED.value)
                    | reached_an_edge,
                )
                .order_by(DeploymentRow.created_at.desc())
                .limit(1)
            ).first()

    def successful_rollback_target(self, current_release: str) -> Deployment:
        # `host_limit IS NULL` keeps canaries out of the automatic choice: one edge
        # reaching a release does not make it a state the fleet was running.
        with self._db.reader() as session:
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
                "previous_release_id": row.previous_release_id,
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
        with self._db.writer() as session:
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
        with self._db.writer() as session:
            session.execute(
                delete(DeploymentRequirementRow).where(
                    DeploymentRequirementRow.kind == kind.value,
                    DeploymentRequirementRow.requested_at <= through,
                )
            )

    def pending(self, kind: DeploymentRequirementKind) -> bool:
        return self.requested_at(kind) is not None

    def requested_at(self, kind: DeploymentRequirementKind) -> datetime | None:
        with self._db.reader() as session:
            row = session.get(DeploymentRequirementRow, kind.value)
            return None if row is None else row.requested_at


class DeploymentOutcomeStore:
    """The running outcome of each kind of convergence, updated in one statement."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def record(self, kind: ConvergenceKind, *, succeeded: bool) -> None:
        now = self._db.now()
        # An upsert, so concurrent recorders never lose an increment to a
        # read-modify-write.
        if succeeded:
            values: dict[str, Any] = {"last_success_at": now, "consecutive_failures": 0}
            update_values: dict[str, Any] = dict(values)
        else:
            values = {"last_failure_at": now, "consecutive_failures": 1}
            update_values = {
                "last_failure_at": now,
                "consecutive_failures": DeploymentOutcomeRow.consecutive_failures + 1,
            }
        with self._db.writer() as session:
            session.execute(
                sqlite_insert(DeploymentOutcomeRow)
                .values(kind=kind.value, **values)
                .on_conflict_do_update(
                    index_elements=[DeploymentOutcomeRow.kind], set_=update_values
                )
            )

    def health(self) -> tuple[ConvergenceHealth, ...]:
        """Every kind, in declaration order; one never recorded reads as empty."""
        with self._db.reader() as session:
            rows = {
                row.kind: row for row in session.scalars(select(DeploymentOutcomeRow))
            }
        return tuple(
            ConvergenceHealth(
                kind=kind,
                last_success_at=row.last_success_at,
                last_failure_at=row.last_failure_at,
                consecutive_failures=row.consecutive_failures,
            )
            if (row := rows.get(kind.value)) is not None
            else ConvergenceHealth(kind=kind)
            for kind in ConvergenceKind
        )


__all__ = [
    "DeploymentOutcomeStore",
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
        with self._db.writer() as session:
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
        with self._db.reader() as session:
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
        with self._db.writer() as session:
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
        with self._db.reader() as session:
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
        with self._db.reader() as session:
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
        with self._db.writer() as session:
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
        with self._db.writer() as session:
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
