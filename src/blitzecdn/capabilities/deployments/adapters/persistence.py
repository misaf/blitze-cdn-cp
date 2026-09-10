"""Persistence for deployment history, the releases it names, and requirements."""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import CursorResult, Result, delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import col

from blitzecdn.capabilities.deployments.adapters.tables import (
    DeploymentRequirementRow,
    DeploymentRow,
    DeploymentTargetRow,
)
from blitzecdn.capabilities.deployments.domain import (
    Deployment,
    DeploymentRequirementKind,
    DeploymentStatus,
    DeploymentTarget,
    TargetPhase,
    TargetStatus,
    require_transition,
)
from blitzecdn.core.domain.runs import AnsibleRun, RunStatus
from blitzecdn.core.exceptions import ConflictError, NotFoundError
from blitzecdn.core.persistence.engine import Database


def _rows_affected(result: Result[Any]) -> int:
    """``rowcount`` for a DML statement.

    ``Session.execute`` is typed as returning ``Result``, which has no
    ``rowcount`` — only the ``CursorResult`` a DML statement actually produces
    does. The narrowing is here rather than at each call site so "how many rows
    did that touch" stays one readable expression.
    """
    return cast("CursorResult[Any]", result).rowcount


class DeploymentStore:
    """Deployment history, and which release each run converged.

    Holds no desired state of its own any more. A deployment names a release,
    the release is immutable and content-addressed, and the release capability
    owns both it and the canonical inputs behind it — so this table went from
    carrying a full copy of every zone and record per row to carrying a
    sixteen-character reference.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    def in_use(self) -> Sequence[str]:
        """Every release some deployment still names.

        Satisfies ``releases.ports.ReleaseReferences``, which pruning asks
        before it removes anything: a release a deployment converged is the
        fleet's way back to that state, and age is no reason to drop it.
        """
        with self._db.session() as session:
            return list(
                session.scalars(select(col(DeploymentRow.release_id)).distinct()).all()
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

    def deployment_release(self, deployment_id: str) -> str:
        """Which release this deployment converged, or is about to."""
        with self._db.session() as session:
            row = session.get(DeploymentRow, deployment_id)
            if row is None:
                raise NotFoundError(f"deployment {deployment_id!r} does not exist")
            return row.release_id

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
        remove the last deployment naming the release the fleet needs to go
        back to. Check-mode runs can
        never be that: rollback selection filters them out.

        They are also the ones that accumulate. The drift timer fires hourly
        and each firing writes a row. They are cheap now that a row names a
        release rather than embedding one — but they are still a row an hour
        that says nothing an operator will read. Run-log retention exists for
        the same reason on the same schedule; this is that policy applied to
        the rows.
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

    def successful_rollback_target(self, current_release: str) -> Deployment:
        # `host_limit IS NULL` keeps canaries out of the automatic choice. A
        # limited run only proves one edge reached that release, so rolling
        # the fleet back to it would converge most edges onto a state they
        # were never running. An operator can still name one explicitly.
        with self._db.session() as session:
            row = session.scalars(
                select(DeploymentRow)
                .where(
                    col(DeploymentRow.status) == DeploymentStatus.SUCCEEDED.value,
                    col(DeploymentRow.check_mode).is_(False),
                    col(DeploymentRow.release_id) != current_release,
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
                "release_id": row.release_id,
                "host_limit": row.host_limit,
                "rollback_of": row.rollback_of,
                "canonical_digest": row.canonical_digest,
                "created_at": row.created_at,
                "started_at": row.started_at,
                "finished_at": row.finished_at,
                "result": row.result,
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

    def clear(self, kind: DeploymentRequirementKind) -> None:
        with self._db.session() as session:
            session.execute(
                delete(DeploymentRequirementRow).where(
                    col(DeploymentRequirementRow.kind) == kind.value
                )
            )

    def pending(self, kind: DeploymentRequirementKind) -> bool:
        with self._db.session() as session:
            return session.get(DeploymentRequirementRow, kind.value) is not None


__all__ = [
    "DeploymentRequirementStore",
    "DeploymentStore",
    "DeploymentTargetStore",
]


class DeploymentTargetStore:
    """Per-edge rollout progress, written under a fence.

    Every write names the generation it was read at. A worker that was paused
    past its job lease and comes back believing it still owns the rollout
    updates zero rows and is told so, rather than recording an outcome for an
    edge somebody else has since converged.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    def claim_generation(self, deployment_id: str) -> int:
        """Take this deployment over, and return the fence that proves it.

        Incrementing rather than setting, so two workers that both try to take
        the same deployment end up with different fences and only the later one
        can write. The increment and the read are one statement for the obvious
        reason: read-then-increment would hand both workers the same number.
        """
        with self._db.session() as session:
            row = session.get(DeploymentRow, deployment_id)
            if row is None:
                raise NotFoundError(f"deployment {deployment_id!r} does not exist")
            row.generation += 1
            session.flush()
            return row.generation

    def generation(self, deployment_id: str) -> int:
        with self._db.session() as session:
            row = session.get(DeploymentRow, deployment_id)
            if row is None:
                raise NotFoundError(f"deployment {deployment_id!r} does not exist")
            return row.generation

    def plan(self, deployment_id: str, edges: Sequence[str], *, fence: int) -> None:
        """Record the edges this rollout aims at, and stamp them with its fence.

        Two jobs, and the second is the takeover. Rows are inserted where
        absent — so resuming an interrupted rollout adds the edges a fleet
        gained since and leaves the progress of the rest alone — and *every*
        row for this deployment is then stamped with the claiming generation.

        That stamp is what makes the fence work. A generation on the deployment
        alone says who claimed it; a generation on each row is what a
        conditional write can name, so the previous owner's next write matches
        nothing and it is told rather than left believing it succeeded.

        Written when the rollout starts rather than when each edge is reached,
        because "three edges were never attempted" has to be a fact on a row.
        """
        with self._db.session() as session:
            existing = {
                row.edge
                for row in session.scalars(
                    select(DeploymentTargetRow).where(
                        col(DeploymentTargetRow.deployment_id) == deployment_id
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
                .where(col(DeploymentTargetRow.deployment_id) == deployment_id)
                .values(fence=fence)
            )

    def targets(self, deployment_id: str) -> list[DeploymentTarget]:
        with self._db.session() as session:
            rows = session.scalars(
                select(DeploymentTargetRow)
                .where(col(DeploymentTargetRow.deployment_id) == deployment_id)
                .order_by(col(DeploymentTargetRow.edge))
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
        """Close out the edges a stopped rollout never reached.

        SKIPPED rather than FAILED, and the distinction is the point: these
        edges are still serving whatever they had and nothing is known to be
        wrong with them. Reporting them as failures would send an operator to
        look at hosts that are fine.
        """
        with self._db.session() as session:
            return _rows_affected(
                session.execute(
                    update(DeploymentTargetRow)
                    .where(
                        col(DeploymentTargetRow.deployment_id) == deployment_id,
                        col(DeploymentTargetRow.status) == TargetStatus.PENDING.value,
                        col(DeploymentTargetRow.fence) == fence,
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
                            col(DeploymentTargetRow.deployment_id) == deployment_id,
                            col(DeploymentTargetRow.edge) == edge,
                            col(DeploymentTargetRow.fence) == fence,
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
