"""What durable background work needs: two stores and a handler table."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, NewType, Protocol

from blitzecdn.capabilities.jobs.domain import (
    Job,
    JobClaim,
    JobOutcome,
    JobPayload,
    JobStatus,
    Schedule,
)
from blitzecdn.capabilities.jobs.domain.observability import (
    QueueStatistics,
    WorkerObservations,
)

__all__ = [
    "JobHandler",
    "JobStore",
    "OperationLeases",
    "OperationRuns",
    "QueuedOperation",
    "ScheduleStore",
    "WorkerHistory",
    "WorkerProgress",
]


@dataclass(frozen=True, slots=True)
class QueuedOperation[P: JobPayload]:
    """One edge operation the API may queue: its payload model and its run.

    The run takes the validated payload and returns its JSON report.
    """

    payload: type[P]
    run: Callable[[P], dict[str, Any]]

    @property
    def kind(self) -> str:
        return self.payload.kind


#: Every edge operation the API may queue, by kind.
OperationRuns = NewType("OperationRuns", dict[str, QueuedOperation[Any]])

#: How long a claim on an operation of each kind should be good for, in
#: seconds, for kinds whose run can outlast the queue's default lease.
OperationLeases = NewType("OperationLeases", dict[str, int])


class JobStore(Protocol):
    """Durable job rows, with every change conditioned on the state read."""

    def enqueue(
        self,
        *,
        kind: str,
        payload: Mapping[str, Any],
        available_at: datetime,
        dedupe_key: str | None = None,
        max_attempts: int = 3,
    ) -> Job | None:
        """Record one job, or ``None`` when its dedupe key is already in flight."""

    def claim(
        self,
        *,
        worker: str,
        now: datetime,
        lease_seconds: int,
        on_abandon: Callable[[Job], None] | None = None,
    ) -> JobClaim | None:
        """Take the oldest runnable job, including lapsed leases, or ``None``.

        A lapsed job with no attempts left is failed instead, and ``on_abandon`` is
        called with it inside the same transaction, so whatever the job was driving
        can be closed out atomically with it.
        """

    def complete(
        self,
        job_id: str,
        fence: int,
        *,
        now: datetime,
        result: Mapping[str, Any] | None = None,
    ) -> bool:
        """Mark a job succeeded with its result, refusing a stale worker."""

    def fail(
        self,
        job_id: str,
        fence: int,
        *,
        now: datetime,
        error: str,
        retry_at: datetime | None,
    ) -> bool:
        """Record a failure, scheduling a retry or finishing the job."""

    def renew(self, job_id: str, fence: int, *, until: datetime) -> bool:
        """Extend a lease this worker still holds."""

    def defer(
        self, job_id: str, fence: int, *, retry_at: datetime, reason: str
    ) -> bool:
        """Return unstarted work to pending and refund only this claim's attempt."""

    def get(self, job_id: str) -> Job:
        """One job; raises ``NotFoundError`` if there is none by that id."""
        ...

    def list_jobs(
        self, *, status: JobStatus | None = None, limit: int = 50
    ) -> Sequence[Job]:
        """The ``limit`` most recent jobs, newest first, optionally of one status."""
        ...

    def prune(self, keep: int) -> int:
        """Drop finished jobs beyond the newest ``keep``."""

    def statistics(self, *, now: datetime) -> QueueStatistics:
        """Aggregate the entire retained queue without loading payloads."""


class WorkerHistory(Protocol):
    def read(self) -> WorkerObservations:
        """Every worker's last heartbeat, and how many records could not be read.

        An unreadable record is counted rather than raised, so one damaged file
        cannot take status reporting down.
        """
        ...


class WorkerProgress(Protocol):
    def start(self, worker_id: str) -> None:
        """Start publishing a heartbeat for ``worker_id``; repeat calls do nothing."""
        ...

    def busy(self, claim: JobClaim) -> None:
        """Report that this worker has taken ``claim``'s job."""
        ...

    def idle(self) -> None:
        """Report that the job in hand finished, and how long it took."""
        ...

    def stop(self) -> None:
        """Stop the heartbeat and withdraw this worker's record."""
        ...


class ScheduleStore(Protocol):
    """When each recurring job is next due."""

    def upsert(self, schedule: Schedule) -> Schedule:
        """Register or update a schedule without disturbing its next firing."""

    def forget(self, names: Sequence[str]) -> int:
        """Remove schedules for jobs no installed feature contributes."""

    def due(self, now: datetime) -> Sequence[Schedule]:
        """Schedules whose next firing is at or before ``now``, soonest first."""
        ...

    def reschedule(
        self, name: str, *, expected: datetime, next_run_at: datetime, ran_at: datetime
    ) -> bool:
        """Move a schedule forward, refusing if another process moved it first."""

    def list_schedules(self) -> Sequence[Schedule]:
        """Every registered schedule, ordered by name."""
        ...


@dataclass(frozen=True, slots=True)
class JobHandler[P: JobPayload]:
    """Does the work for one kind of job, given only its validated payload.

    ``handle`` answers with a
    :data:`~blitzecdn.capabilities.jobs.domain.JobOutcome`; raising is for the
    unexpected. The queue validates the stored payload against ``payload``
    first, so a handler never sees a payload of the wrong shape.
    """

    payload: type[P]
    handle: Callable[[P], JobOutcome]
    #: Called when a job of this kind is abandoned: every attempt ended with its
    #: worker gone, so ``handle`` never got to record an outcome. It runs inside
    #: the transaction that fails the job, with the payload and the job's error,
    #: and must only write state; it must not do external work.
    abandon: Callable[[P, str], None] | None = None

    @property
    def kind(self) -> str:
        return self.payload.kind
