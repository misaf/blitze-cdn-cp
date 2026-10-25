"""How durable background work is wired: cadences, leases and worker identity."""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping

from blitzecdn.capabilities.jobs.ports import JobHandler, JobStore, ScheduleStore
from blitzecdn.capabilities.jobs.service import (
    JobQueue,
    JobScheduler,
    ScheduleSpec,
)
from blitzecdn.core.features import ScheduledJob
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.ports.clock import Clock, system_clock

__all__ = [
    "build_job_queue",
    "build_job_scheduler",
    "schedule_specs",
    "worker_identity",
]


def worker_identity() -> str:
    """Who holds a lease, as host and process an operator can go and inspect."""
    return f"{socket.gethostname()}:{os.getpid()}"


def schedule_specs(jobs: Mapping[str, ScheduledJob]) -> tuple[ScheduleSpec, ...]:
    """Translate contributed jobs into schedules.

    A zero interval disables the job; a zero lease means twice the interval.
    """
    return tuple(
        ScheduleSpec(
            job.name,
            interval_seconds=job.interval_seconds,
            lease_seconds=job.lease_seconds or job.interval_seconds * 2,
            jitter_seconds=job.jitter_seconds,
        )
        for job in sorted(jobs.values(), key=lambda item: item.name)
        if job.interval_seconds
    )


def build_job_queue(
    *,
    jobs: JobStore,
    retention: int,
    handlers: Mapping[str, JobHandler] | None = None,
    clock: Clock = system_clock,
) -> JobQueue:
    """Wire the queue; ``handlers`` is empty for a publish-only process like the API."""
    return JobQueue(
        jobs=jobs,
        handlers=handlers if handlers is not None else {},
        worker_id=worker_identity(),
        retention=retention,
        clock=clock,
    )


def build_job_scheduler(
    *,
    schedules: ScheduleStore,
    queue: JobQueue,
    uow: UnitOfWork,
    clock: Clock = system_clock,
) -> JobScheduler:
    """Wire the scheduler to the schedule table and the queue it publishes to."""
    return JobScheduler(schedules=schedules, queue=queue, uow=uow, clock=clock)
