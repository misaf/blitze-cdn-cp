"""Durable background work, on the database the control plane already has.

No broker: one deployment at a time and a few hourly jobs need durability,
single-flight and recovery from a dead worker, each a column on a row. See
:mod:`~blitzecdn.capabilities.jobs.domain.job`. Delivery is at least once;
handlers tolerate duplicates.
"""

from __future__ import annotations

from blitzecdn.capabilities.jobs.domain import Job, JobClaim, JobStatus, Schedule
from blitzecdn.capabilities.jobs.service import (
    DEPLOYMENT_JOB,
    SCHEDULED_JOB,
    JobQueue,
    JobRunner,
    JobScheduler,
    ScheduleSpec,
)

__all__ = [
    "DEPLOYMENT_JOB",
    "SCHEDULED_JOB",
    "Job",
    "JobClaim",
    "JobQueue",
    "JobRunner",
    "JobScheduler",
    "JobStatus",
    "Schedule",
    "ScheduleSpec",
]
