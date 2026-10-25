"""Durable background work, on the database the control plane already has.

No broker and no separate scheduler. There is one deployment at a time; the
deployment lock says so. Recurring work is a handful of jobs on intervals
measured in hours. What the workload needs is durability across a restart,
single-flight for the recurring jobs, and a worker whose disappearance does not
strand the work it was holding — and each of those is a column on a row, on
the database that already holds every other durable fact about this
installation.

The properties that make it safe are in
:mod:`~blitzecdn.capabilities.jobs.domain.job`: availability, leasing and
fencing. The rule the whole thing rests on is that duplicate delivery is the
handler's problem and the handlers were built for it — `run_queued` checks the
deployment is still queued, and a maintenance run is idempotent. Exactly-once
is not offered, because on one database with no distributed transaction it
could not be offered honestly.
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
