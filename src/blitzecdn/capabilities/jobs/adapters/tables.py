"""Durable background work, and when recurring work is next due."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Index, text
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from blitzecdn.core.persistence.tables import Base, UtcDateTime, utcnow


class JobRow(Base):
    """One unit of durable background work."""

    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')",
            name="jobs_status_check",
        ),
        CheckConstraint("attempts >= 0", name="jobs_attempts_check"),
        CheckConstraint("max_attempts >= 1", name="jobs_max_attempts_check"),
        # Single-flight, enforced by a partial unique index binding only while the
        # job is unfinished.
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

    id: Mapped[str] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column()
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column()
    dedupe_key: Mapped[str | None] = mapped_column()
    attempts: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=3)
    available_at: Mapped[datetime] = mapped_column(UtcDateTime)
    leased_until: Mapped[datetime | None] = mapped_column(UtcDateTime)
    leased_by: Mapped[str | None] = mapped_column()
    #: Incremented on every claim. A worker completes a job by presenting the
    #: value its claim returned; a stale worker's write matches no row.
    fence: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    last_error: Mapped[str | None] = mapped_column()
    #: What a succeeded job's handler reported, for whoever polls for it.
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class ScheduleRow(Base):
    """When one recurring job may next be enqueued, durable across outages."""

    __tablename__ = "job_schedules"
    __table_args__ = (
        CheckConstraint("interval_seconds >= 1", name="job_schedules_interval_check"),
        CheckConstraint("lease_seconds >= 1", name="job_schedules_lease_check"),
        Index("ix_job_schedules_next_run_at", "next_run_at"),
    )

    name: Mapped[str] = mapped_column(primary_key=True)
    interval_seconds: Mapped[int] = mapped_column()
    jitter_seconds: Mapped[int] = mapped_column(default=0)
    lease_seconds: Mapped[int] = mapped_column()
    next_run_at: Mapped[datetime] = mapped_column(UtcDateTime)
    last_run_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
