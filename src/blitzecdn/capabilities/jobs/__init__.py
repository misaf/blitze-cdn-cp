"""Durable background work, on the database the control plane already has.

No broker: one deployment at a time and a few hourly jobs need durability,
single-flight and recovery from a dead worker, each a column on a row. See
:mod:`~blitzecdn.capabilities.jobs.domain.job`. Delivery is at least once;
handlers tolerate duplicates.
"""

from __future__ import annotations

from blitzecdn.capabilities.jobs.domain import (
    DeploymentJob,
    Job,
    JobClaim,
    JobPayload,
    JobStatus,
    Schedule,
    ScheduledFiring,
)
from blitzecdn.capabilities.jobs.domain.observability import (
    QueueStatistics,
    WorkerHeartbeat,
)
from blitzecdn.capabilities.jobs.ports import QueuedOperation, WorkerHistory
from blitzecdn.capabilities.jobs.service import (
    JobQueue,
    JobRunner,
    JobScheduler,
    OperationService,
    ScheduleSpec,
)

__all__ = [
    "DeploymentJob",
    "Job",
    "JobClaim",
    "JobPayload",
    "JobQueue",
    "JobRunner",
    "JobScheduler",
    "JobStatus",
    "OperationService",
    "QueueStatistics",
    "QueuedOperation",
    "Schedule",
    "ScheduleSpec",
    "ScheduledFiring",
    "WorkerHeartbeat",
    "WorkerHistory",
]
