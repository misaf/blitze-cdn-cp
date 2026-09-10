"""What durable background work needs from the world.

Two stores and one handler table. The stores are declared here rather than
imported because this capability is testable without a database — the queue's
rules are about leases, fences and backoff, and a fake store exercises all of
them — and the handler table is declared here because the worker must not know
what any job *does*.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

from blitzecdn.capabilities.jobs.domain import Job, JobClaim, JobStatus, Schedule

__all__ = ["JobHandler", "JobStore", "ScheduleStore"]


class JobStore(Protocol):
    """Durable job rows, and the atomic operations the queue is built from.

    Every method that changes a job takes the state it expects to find, so a
    concurrent worker loses rather than overwrites. That is the store's whole
    responsibility: the queue above it decides *what* should happen and this
    decides whether the row it happens to was still the row that was read.
    """

    def enqueue(
        self,
        *,
        kind: str,
        payload: Mapping[str, Any],
        available_at: datetime,
        dedupe_key: str | None = None,
        max_attempts: int = 3,
    ) -> Job | None:
        """Record one job, or ``None`` when its dedupe key is already in flight.

        ``None`` rather than an exception: a scheduled job that is still
        running when its next firing comes round is the ordinary case, not an
        error, and a caller that had to catch something for it would end up
        catching it in a place that could not tell that case from a real one.
        """

    def claim(
        self, *, worker: str, now: datetime, lease_seconds: int
    ) -> JobClaim | None:
        """Take the oldest runnable job, or ``None`` when there is none.

        Runnable means pending and due, *or* running with an expired lease —
        the second being crash recovery, which needs no separate sweep because
        it is the same question asked of the same row.
        """

    def complete(self, job_id: str, fence: int, *, now: datetime) -> bool:
        """Mark a job succeeded, refusing a worker whose lease was taken over."""

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

    def get(self, job_id: str) -> Job: ...

    def list_jobs(
        self, *, status: JobStatus | None = None, limit: int = 50
    ) -> Sequence[Job]: ...

    def prune(self, keep: int) -> int:
        """Drop finished jobs beyond the newest ``keep``."""


class ScheduleStore(Protocol):
    """When each recurring job is next due."""

    def upsert(self, schedule: Schedule) -> Schedule:
        """Register or update a schedule without disturbing its next firing.

        Registration happens on every process start, so this must be idempotent
        *and* must not move ``next_run_at`` — a controller restarted every few
        minutes during a deployment would otherwise push every schedule
        permanently into the future and run nothing.
        """

    def forget(self, names: Sequence[str]) -> int:
        """Remove schedules for jobs no installed plugin contributes."""

    def due(self, now: datetime) -> Sequence[Schedule]: ...

    def reschedule(
        self, name: str, *, expected: datetime, next_run_at: datetime, ran_at: datetime
    ) -> bool:
        """Move a schedule forward, refusing if another process moved it first.

        The compare-and-swap that makes two controllers safe to run at once:
        both may see the same schedule as due, and exactly one of them wins the
        right to enqueue it.
        """

    def list_schedules(self) -> Sequence[Schedule]: ...


class JobHandler(Protocol):
    """What actually does the work for one kind of job.

    Takes the payload and nothing else. The worker resolves a kind to one of
    these and knows no more than that — which is what lets a deployment and a
    plugin-contributed maintenance run share one loop, one lease and one
    fencing rule without the loop knowing what either of them is.
    """

    def __call__(self, payload: Mapping[str, Any]) -> None: ...
