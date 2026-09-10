"""The job and schedule tables, and the compare-and-swap each write is.

Every write here is conditional. That is the whole of how two controller
processes stay out of each other's way without a lock: SQLite's ``BEGIN
IMMEDIATE`` serialises writers, and each statement names the state it expected
to find, so the loser of a race updates zero rows and is told so rather than
overwriting the winner's work.

The claim is the one to read closely. It selects a runnable job and then
updates that row *by id and by fence*, so a second process that selected the
same row in the same instant finds the fence already moved and takes nothing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import CursorResult, Result, delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import col

from blitzecdn.capabilities.jobs.adapters.tables import JobRow, ScheduleRow
from blitzecdn.capabilities.jobs.domain import Job, JobClaim, JobStatus, Schedule
from blitzecdn.core.exceptions import NotFoundError
from blitzecdn.core.persistence.engine import Database

__all__ = ["JobStore", "ScheduleStore"]


def _rows_affected(result: Result[Any]) -> int:
    return cast("CursorResult[Any]", result).rowcount


class JobStore:
    def __init__(self, database: Database) -> None:
        self._db = database

    def enqueue(
        self,
        *,
        kind: str,
        payload: Mapping[str, Any],
        available_at: datetime,
        dedupe_key: str | None = None,
        max_attempts: int = 3,
    ) -> Job | None:
        row = JobRow(
            id=uuid4().hex,
            kind=kind,
            payload=dict(payload),
            status=JobStatus.PENDING.value,
            dedupe_key=dedupe_key,
            attempts=0,
            max_attempts=max_attempts,
            available_at=available_at,
            fence=0,
            created_at=self._db.now(),
        )
        try:
            with self._db.session() as session:
                session.add(row)
                session.flush()
                return _job(row)
        except IntegrityError:
            # The partial unique index refused it: a job with this dedupe key
            # is still pending or running. Caught rather than checked for,
            # because a check followed by an insert is a race with exactly the
            # window this index exists to close.
            return None

    def claim(
        self, *, worker: str, now: datetime, lease_seconds: int
    ) -> JobClaim | None:
        with self._db.session() as session:
            row = session.scalars(
                select(JobRow)
                .where(
                    or_(
                        (col(JobRow.status) == JobStatus.PENDING.value)
                        & (col(JobRow.available_at) <= now),
                        # Crash recovery, and not a separate sweep: a running
                        # job whose lease has expired is a job whose worker
                        # stopped renewing, whether it died, hung or was
                        # killed. Nothing has to observe the death.
                        (col(JobRow.status) == JobStatus.RUNNING.value)
                        & (col(JobRow.leased_until).is_not(None))
                        & (col(JobRow.leased_until) <= now),
                    )
                )
                .order_by(col(JobRow.available_at), col(JobRow.created_at))
                .limit(1)
            ).first()
            if row is None:
                return None
            fence = row.fence + 1
            taken = _rows_affected(
                session.execute(
                    update(JobRow)
                    .where(
                        col(JobRow.id) == row.id,
                        # By the fence it was read at. Two processes selecting
                        # the same row in the same instant both try this, and
                        # the second matches nothing.
                        col(JobRow.fence) == row.fence,
                    )
                    .values(
                        status=JobStatus.RUNNING.value,
                        fence=fence,
                        attempts=row.attempts + 1,
                        leased_by=worker,
                        leased_until=now + _seconds(lease_seconds),
                    )
                )
            )
            if not taken:
                return None
            claimed = session.get(JobRow, row.id)
            if claimed is None:  # pragma: no cover - the row was just updated
                return None
            return JobClaim(job=_job(claimed), fence=fence)

    def complete(self, job_id: str, fence: int, *, now: datetime) -> bool:
        with self._db.session() as session:
            return bool(
                _rows_affected(
                    session.execute(
                        update(JobRow)
                        .where(
                            col(JobRow.id) == job_id,
                            col(JobRow.fence) == fence,
                            col(JobRow.status) == JobStatus.RUNNING.value,
                        )
                        .values(
                            status=JobStatus.SUCCEEDED.value,
                            finished_at=now,
                            leased_until=None,
                            leased_by=None,
                            # Cleared so the partial unique index stops binding:
                            # a finished job must not keep the next firing of
                            # the same scheduled work out of the queue.
                            dedupe_key=None,
                        )
                    )
                )
            )

    def fail(
        self,
        job_id: str,
        fence: int,
        *,
        now: datetime,
        error: str,
        retry_at: datetime | None,
    ) -> bool:
        retrying = retry_at is not None
        values: dict[str, Any] = {
            "status": (JobStatus.PENDING.value if retrying else JobStatus.FAILED.value),
            "last_error": error,
            "leased_until": None,
            "leased_by": None,
        }
        if retrying:
            values["available_at"] = retry_at
        else:
            values["finished_at"] = now
            values["dedupe_key"] = None
        with self._db.session() as session:
            return bool(
                _rows_affected(
                    session.execute(
                        update(JobRow)
                        .where(
                            col(JobRow.id) == job_id,
                            col(JobRow.fence) == fence,
                            col(JobRow.status) == JobStatus.RUNNING.value,
                        )
                        .values(**values)
                    )
                )
            )

    def renew(self, job_id: str, fence: int, *, until: datetime) -> bool:
        with self._db.session() as session:
            return bool(
                _rows_affected(
                    session.execute(
                        update(JobRow)
                        .where(
                            col(JobRow.id) == job_id,
                            col(JobRow.fence) == fence,
                            col(JobRow.status) == JobStatus.RUNNING.value,
                        )
                        .values(leased_until=until)
                    )
                )
            )

    def get(self, job_id: str) -> Job:
        with self._db.session() as session:
            row = session.get(JobRow, job_id)
            if row is None:
                raise NotFoundError(f"job {job_id!r} does not exist")
            return _job(row)

    def list_jobs(
        self, *, status: JobStatus | None = None, limit: int = 50
    ) -> list[Job]:
        with self._db.session() as session:
            statement = select(JobRow).order_by(col(JobRow.created_at).desc())
            if status is not None:
                statement = statement.where(col(JobRow.status) == status.value)
            return [_job(row) for row in session.scalars(statement.limit(limit)).all()]

    def prune(self, keep: int) -> int:
        """Drop finished jobs beyond the newest ``keep``.

        Finished only. A pending job is work nobody has done and a running one
        is work somebody is doing, and a retention policy that could remove
        either would be a retention policy that loses deployments.
        """
        finished = (JobStatus.SUCCEEDED.value, JobStatus.FAILED.value)
        with self._db.session() as session:
            survivors = (
                select(col(JobRow.id))
                .where(col(JobRow.status).in_(finished))
                .order_by(col(JobRow.created_at).desc())
                .limit(keep)
                .scalar_subquery()
            )
            return _rows_affected(
                session.execute(
                    delete(JobRow).where(
                        col(JobRow.status).in_(finished),
                        col(JobRow.id).not_in(survivors),
                    )
                )
            )


class ScheduleStore:
    def __init__(self, database: Database) -> None:
        self._db = database

    def upsert(self, schedule: Schedule) -> Schedule:
        """Register a schedule, leaving an existing one's next firing alone.

        Registration runs on every process start. Writing ``next_run_at``
        unconditionally would mean a controller restarted every few minutes —
        which is what an upgrade or a crash loop looks like — pushed every
        schedule permanently into the future and ran nothing at all.
        """
        with self._db.session() as session:
            row = session.get(ScheduleRow, schedule.name)
            if row is None:
                session.add(
                    ScheduleRow(
                        name=schedule.name,
                        interval_seconds=schedule.interval_seconds,
                        jitter_seconds=schedule.jitter_seconds,
                        lease_seconds=schedule.lease_seconds,
                        next_run_at=schedule.next_run_at,
                        last_run_at=schedule.last_run_at,
                    )
                )
                return schedule
            row.interval_seconds = schedule.interval_seconds
            row.jitter_seconds = schedule.jitter_seconds
            row.lease_seconds = schedule.lease_seconds
            return _schedule(row)

    def forget(self, names: Sequence[str]) -> int:
        """Remove every schedule not in ``names``.

        A plugin uninstalled while its schedule was registered would otherwise
        leave a row the worker enqueues forever and resolves to nothing — one
        `NotFoundError` per interval, about a package the operator removed on
        purpose.
        """
        with self._db.session() as session:
            return _rows_affected(
                session.execute(
                    delete(ScheduleRow).where(col(ScheduleRow.name).not_in(names))
                )
            )

    def due(self, now: datetime) -> list[Schedule]:
        with self._db.session() as session:
            rows = session.scalars(
                select(ScheduleRow)
                .where(col(ScheduleRow.next_run_at) <= now)
                .order_by(col(ScheduleRow.next_run_at))
            ).all()
            return [_schedule(row) for row in rows]

    def reschedule(
        self,
        name: str,
        *,
        expected: datetime,
        next_run_at: datetime,
        ran_at: datetime,
    ) -> bool:
        with self._db.session() as session:
            return bool(
                _rows_affected(
                    session.execute(
                        update(ScheduleRow)
                        .where(
                            col(ScheduleRow.name) == name,
                            # The compare-and-swap. Two controllers may both see
                            # this schedule as due; exactly one moves it, and
                            # only that one goes on to enqueue the work.
                            col(ScheduleRow.next_run_at) == expected,
                        )
                        .values(next_run_at=next_run_at, last_run_at=ran_at)
                    )
                )
            )

    def list_schedules(self) -> list[Schedule]:
        with self._db.session() as session:
            rows = session.scalars(
                select(ScheduleRow).order_by(col(ScheduleRow.name))
            ).all()
            return [_schedule(row) for row in rows]


def _seconds(value: int) -> timedelta:
    return timedelta(seconds=value)


def _job(row: JobRow) -> Job:
    return Job.model_validate(
        {
            "id": row.id,
            "kind": row.kind,
            "payload": row.payload,
            "status": row.status,
            "dedupe_key": row.dedupe_key,
            "attempts": row.attempts,
            "max_attempts": row.max_attempts,
            "available_at": row.available_at,
            "leased_until": row.leased_until,
            "leased_by": row.leased_by,
            "fence": row.fence,
            "created_at": row.created_at,
            "finished_at": row.finished_at,
            "last_error": row.last_error,
        }
    )


def _schedule(row: ScheduleRow) -> Schedule:
    return Schedule.model_validate(
        {
            "name": row.name,
            "interval_seconds": row.interval_seconds,
            "jitter_seconds": row.jitter_seconds,
            "lease_seconds": row.lease_seconds,
            "next_run_at": row.next_run_at,
            "last_run_at": row.last_run_at,
        }
    )
