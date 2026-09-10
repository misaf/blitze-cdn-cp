"""Durable background work, and when recurring work is next due.

Part of the one description of what is on disk. The rules `core.persistence`
sets out still hold — a column per queryable fact, JSON for a value object no
query reaches — and every column here is queried: a claim filters on status and
availability, a lease sweep on expiry, and the single-flight guarantee is a
unique index rather than a check somebody remembered to write.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Column, Index, Integer, String, text
from sqlalchemy.dialects.sqlite import JSON
from sqlmodel import Field

from blitzecdn.core.persistence.tables import Base, UtcDateTime, utcnow


class JobRow(Base, table=True):
    """One unit of durable background work."""

    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')",
            name="jobs_status_check",
        ),
        CheckConstraint("attempts >= 0", name="jobs_attempts_check"),
        CheckConstraint("max_attempts >= 1", name="jobs_max_attempts_check"),
        # The single-flight guarantee, enforced by the database rather than by
        # application code. Partial, so it binds only while a job is unfinished:
        # a scheduled job that ran an hour ago must not stop the next firing,
        # and a finished row keeping its key would do exactly that. This is what
        # the Redis `SET NX` key with a TTL was, moved to where the work is and
        # made incapable of outliving it.
        Index(
            "ix_jobs_dedupe_key_unfinished",
            "dedupe_key",
            unique=True,
            sqlite_where=text("status IN ('pending', 'running')"),
        ),
        # The claim query: the runnable jobs, oldest first.
        Index("ix_jobs_status_available_at", "status", "available_at"),
        # The lease sweep: running jobs whose lease has expired.
        Index("ix_jobs_leased_until", "leased_until"),
    )

    id: str = Field(sa_column=Column(String, primary_key=True))
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict, sa_type=JSON)
    status: str
    dedupe_key: str | None = None
    attempts: int = Field(sa_column=Column(Integer, nullable=False, default=0))
    max_attempts: int = Field(sa_column=Column(Integer, nullable=False, default=3))
    available_at: datetime = Field(sa_type=UtcDateTime)
    leased_until: datetime | None = Field(default=None, sa_type=UtcDateTime)
    leased_by: str | None = None
    #: Incremented on every claim. A worker completes a job by presenting the
    #: value its claim returned; a stale worker's write matches no row.
    fence: int = Field(sa_column=Column(Integer, nullable=False, default=0))
    created_at: datetime = Field(
        default_factory=utcnow, sa_type=UtcDateTime, index=True
    )
    finished_at: datetime | None = Field(default=None, sa_type=UtcDateTime)
    last_error: str | None = None


class ScheduleRow(Base, table=True):
    """When one recurring job is next allowed to be enqueued.

    Durable so an outage cannot silently swallow a firing: a controller that
    starts and finds a schedule overdue enqueues it, rather than resetting an
    in-memory timer and waiting out another whole interval.
    """

    __tablename__ = "job_schedules"
    __table_args__ = (
        CheckConstraint("interval_seconds >= 1", name="job_schedules_interval_check"),
        CheckConstraint("lease_seconds >= 1", name="job_schedules_lease_check"),
        Index("ix_job_schedules_next_run_at", "next_run_at"),
    )

    name: str = Field(sa_column=Column(String, primary_key=True))
    interval_seconds: int
    jitter_seconds: int = 0
    lease_seconds: int
    next_run_at: datetime = Field(sa_type=UtcDateTime)
    last_run_at: datetime | None = Field(default=None, sa_type=UtcDateTime)
