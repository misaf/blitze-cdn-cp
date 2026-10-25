"""One unit of durable background work, and the rules its lifecycle obeys.

``available_at`` is when a job may next be claimed, carrying delay and backoff.
A claim sets ``leased_until``; a dead worker stops renewing and its job becomes
claimable with nothing having to notice. ``fence`` increments per claim and
finishing requires the claim's token, so a worker paused past its lease cannot
complete a job someone else re-ran.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "DONE",
    "MAX_BACKOFF_SECONDS",
    "Defer",
    "Done",
    "Fail",
    "Job",
    "JobClaim",
    "JobOutcome",
    "JobStatus",
    "Retry",
    "backoff_for",
    "is_terminal",
]

#: The default lease: long enough for a convergence, short enough to recover.
DEFAULT_LEASE_SECONDS = 900

#: Retry backoff ceiling. Retried failures are transient; a job out of
#: attempts fails and is reported rather than retrying forever.
MAX_BACKOFF_SECONDS = 300

_BASE_BACKOFF_SECONDS = 2


class JobStatus(StrEnum):
    """Where a job is. Four states, and only two of them are terminal."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


TERMINAL_STATUSES = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED})


def is_terminal(status: JobStatus) -> bool:
    return status in TERMINAL_STATUSES


def backoff_for(attempt: int) -> timedelta:
    """Exponential backoff from two seconds, capped; no jitter with one worker."""
    seconds = min(_BASE_BACKOFF_SECONDS ** max(attempt, 1), MAX_BACKOFF_SECONDS)
    return timedelta(seconds=seconds)


class Job(BaseModel):
    """A durable unit of work, as it stands right now."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    #: The handler kind, as a string so contributed job kinds need no enum.
    kind: str
    #: The handler's arguments as JSON: an id or a name, never desired state.
    payload: dict[str, Any] = Field(default_factory=dict)
    status: JobStatus = JobStatus.PENDING
    #: Single-flight key, or ``None``; unique among unfinished jobs.
    dedupe_key: str | None = None
    #: Claims that executed or disappeared without recording an outcome.
    #: Explicit deferral before execution refunds its claim's increment.
    attempts: int = 0
    max_attempts: int = 3
    available_at: datetime
    leased_until: datetime | None = None
    #: Which worker holds the current lease, for an operator reading the table.
    leased_by: str | None = None
    #: Incremented on every claim. A worker proves it still owns the job by
    #: presenting the value it was given; a stale one is refused.
    fence: int = 0
    created_at: datetime
    finished_at: datetime | None = None
    last_error: str | None = None

    @property
    def exhausted(self) -> bool:
        """Whether a failure now is the last one this job gets."""
        return self.attempts >= self.max_attempts

    def claimable_at(self, now: datetime) -> bool:
        """Whether this job may be claimed: due and pending, or its lease lapsed."""
        if self.status is JobStatus.PENDING:
            return self.available_at <= now
        if self.status is JobStatus.RUNNING:
            return self.leased_until is not None and self.leased_until <= now
        return False


class JobClaim(BaseModel):
    """A job and the fence token proving this worker runs it, returned together."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job: Job
    fence: int

    @classmethod
    def of(cls, job: Job) -> Self:
        return cls(job=job, fence=job.fence)


@dataclass(frozen=True, slots=True)
class Done:
    """The work is finished."""


@dataclass(frozen=True, slots=True)
class Retry:
    """Failed in a way another attempt may not; back off, spending the attempt."""

    reason: str


@dataclass(frozen=True, slots=True)
class Defer:
    """Could not start, e.g. the deployment lock is held; requeue without spending."""

    reason: str


@dataclass(frozen=True, slots=True)
class Fail:
    """Failed, and a retry would meet the same answer; fail at once."""

    reason: str


#: What a handler reports; an escaping exception is retried like ``Retry``.
JobOutcome = Done | Retry | Defer | Fail

#: The one ``Done``, so a handler can say ``return DONE``.
DONE = Done()
