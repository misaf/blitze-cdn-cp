"""The values durable background work is made of."""

from __future__ import annotations

from blitzecdn.capabilities.jobs.domain.job import (
    DEFAULT_LEASE_SECONDS,
    MAX_BACKOFF_SECONDS,
    TERMINAL_STATUSES,
    DeferredJobError,
    Job,
    JobClaim,
    JobStatus,
    PermanentJobError,
    backoff_for,
    is_terminal,
)
from blitzecdn.capabilities.jobs.domain.schedule import Schedule, next_due

__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "MAX_BACKOFF_SECONDS",
    "TERMINAL_STATUSES",
    "DeferredJobError",
    "Job",
    "JobClaim",
    "JobStatus",
    "PermanentJobError",
    "Schedule",
    "backoff_for",
    "is_terminal",
    "next_due",
]
