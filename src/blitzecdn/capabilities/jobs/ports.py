"""What durable background work needs: two stores and a handler table."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

from blitzecdn.capabilities.jobs.domain import (
    Job,
    JobClaim,
    JobOutcome,
    JobStatus,
    Schedule,
)

__all__ = ["JobHandler", "JobStore", "ScheduleStore"]


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
        self, *, worker: str, now: datetime, lease_seconds: int
    ) -> JobClaim | None:
        """Take the oldest runnable job, including lapsed leases, or ``None``."""

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

    def defer(
        self, job_id: str, fence: int, *, retry_at: datetime, reason: str
    ) -> bool:
        """Return unstarted work to pending and refund only this claim's attempt."""

    def get(self, job_id: str) -> Job: ...

    def list_jobs(
        self, *, status: JobStatus | None = None, limit: int = 50
    ) -> Sequence[Job]: ...

    def prune(self, keep: int) -> int:
        """Drop finished jobs beyond the newest ``keep``."""


class ScheduleStore(Protocol):
    """When each recurring job is next due."""

    def upsert(self, schedule: Schedule) -> Schedule:
        """Register or update a schedule without disturbing its next firing."""

    def forget(self, names: Sequence[str]) -> int:
        """Remove schedules for jobs no installed feature contributes."""

    def due(self, now: datetime) -> Sequence[Schedule]: ...

    def reschedule(
        self, name: str, *, expected: datetime, next_run_at: datetime, ran_at: datetime
    ) -> bool:
        """Move a schedule forward, refusing if another process moved it first."""

    def list_schedules(self) -> Sequence[Schedule]: ...


class JobHandler(Protocol):
    """Does the work for one kind of job, given only its payload.

    Answers with a :data:`~blitzecdn.capabilities.jobs.domain.JobOutcome`; raising
    is for the unexpected.
    """

    def __call__(self, payload: Mapping[str, Any]) -> JobOutcome: ...
