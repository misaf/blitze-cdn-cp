"""One unit of durable background work, and the rules its lifecycle obeys.

A job is a row. That is the whole of the design, and it is a deliberate answer
to what was here before: a Redis broker with Dramatiq actors on one side and an
APScheduler timer on the other, which put three moving parts and one more
network service under a control plane whose entire durable state already lives
in a single SQLite file.

The workload does not need more than a row. There is one deployment at a time —
a cross-process lock says so — and a handful of scheduled jobs on intervals
measured in hours. What that workload *does* need is exactly what a broker
made hard: work that survives a restart of the process that queued it, a
scheduled job that cannot run twice at once, and a worker whose lease expiring
means another worker may take over without the first one being able to finish
underneath it.

Three ideas do all of that, and each is one column.

**Availability.** ``available_at`` is when a job may next be claimed. It carries
the initial delay, the backoff after a failure, and nothing else — a job is
runnable when its time has come and no queue ordering is needed beyond that.

**Leasing.** A claim sets ``leased_until`` and hands the worker a token. A
worker that dies simply stops renewing, and the lease expires; nothing has to
notice the death for the work to become claimable again.

**Fencing.** ``fence`` increments on every claim, and finishing a job requires
the token the claim returned. A worker that was paused past its lease — a long
GC, a suspended container, a network partition it did not notice — comes back
holding a stale token and is refused, so it cannot mark as succeeded a job
another worker has since re-run. This is the part a lease alone does not give
you, and the part that makes "at least once" safe to rely on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "MAX_BACKOFF_SECONDS",
    "Job",
    "JobClaim",
    "JobStatus",
    "backoff_for",
    "is_terminal",
]

#: How long a claim is good for when the caller expresses no opinion. Long
#: enough that an ordinary convergence finishes inside it, short enough that a
#: dead worker's job is claimable again within a maintenance window.
DEFAULT_LEASE_SECONDS = 900

#: The ceiling on retry backoff. Bounded rather than unbounded because the
#: failures this retries are transient by assumption — a busy lock, an edge
#: that was briefly unreachable — and a job that has been sleeping for hours is
#: one nobody is still waiting for. A job that exhausts its attempts fails
#: permanently and says so, which is a better answer than one that retries
#: forever and is therefore never reported.
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
    """How long to wait before a failed job's next attempt.

    Exponential from two seconds, capped at :data:`MAX_BACKOFF_SECONDS`. No
    jitter, and that is not an oversight: there is one worker process and the
    thundering-herd problem jitter solves does not exist here. Adding it would
    make the retry schedule unpredictable in tests for no benefit anyone can
    point at in production.
    """
    seconds = min(_BASE_BACKOFF_SECONDS ** max(attempt, 1), MAX_BACKOFF_SECONDS)
    return timedelta(seconds=seconds)


class Job(BaseModel):
    """A durable unit of work, as it stands right now."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    #: What this job *is*, resolved by the worker against its handler table.
    #: A string rather than an enum because a plugin this repository has never
    #: heard of contributes work through the scheduled-job kind, and a closed
    #: set here would mean core enumerating what it cannot know.
    kind: str
    #: The handler's arguments, as JSON. Small on purpose: a payload that
    #: carried desired state would be a second copy of it, so a deployment job
    #: carries a deployment id and a scheduled job carries a name.
    payload: dict[str, Any] = Field(default_factory=dict)
    status: JobStatus = JobStatus.PENDING
    #: What makes this job single-flight, or ``None`` for one that is not.
    #:
    #: Unique across jobs that have not finished, so enqueueing a scheduled job
    #: while the last copy is still pending or running is refused by the
    #: database rather than by a check somebody remembered to write. This is
    #: what the Redis ``SET NX`` key did, moved to where the work already is —
    #: and unlike that key it cannot outlive the job it guards, because it *is*
    #: a column on the job.
    dedupe_key: str | None = None
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
        """Whether this job may be claimed at ``now``.

        Two ways in. A pending job whose time has come, and a running job whose
        lease has expired — the second is crash recovery, and it needs no
        separate sweep or startup pass precisely because it is the same
        question asked of the same row.
        """
        if self.status is JobStatus.PENDING:
            return self.available_at <= now
        if self.status is JobStatus.RUNNING:
            return self.leased_until is not None and self.leased_until <= now
        return False


class JobClaim(BaseModel):
    """A job, and the proof that this worker is the one running it.

    The two travel together because neither is useful alone: a worker holding a
    job without its fence cannot complete it, and a fence without the job names
    nothing. Returning them as one value is what stops a caller from finishing
    a job with a token it read back out of the table afterwards — by which time
    it may belong to somebody else.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    job: Job
    fence: int

    @classmethod
    def of(cls, job: Job) -> Self:
        return cls(job=job, fence=job.fence)


def utcnow() -> datetime:
    return datetime.now(UTC)
