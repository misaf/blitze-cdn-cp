"""The values durable background work is made of."""

from __future__ import annotations

from blitzecdn.capabilities.jobs.domain.job import (
    DEFAULT_LEASE_SECONDS,
    MAX_BACKOFF_SECONDS,
    TERMINAL_STATUSES,
    Job,
    JobClaim,
    JobStatus,
    backoff_for,
    is_terminal,
    utcnow,
)
from blitzecdn.capabilities.jobs.domain.schedule import Schedule, next_due

__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "MAX_BACKOFF_SECONDS",
    "TERMINAL_STATUSES",
    "Job",
    "JobClaim",
    "JobStatus",
    "Schedule",
    "backoff_for",
    "is_terminal",
    "next_due",
    "utcnow",
]
