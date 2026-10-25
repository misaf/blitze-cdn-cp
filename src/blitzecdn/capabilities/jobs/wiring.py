"""How durable background work is wired to this installation.

Two decisions live here: what a scheduled job's cadence and lease come out as,
and who the worker calls itself. Which handler runs which kind of job binds
other capabilities' services to this one, so the application composition root
supplies that table.
"""

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
    """Who holds a lease, in a form an operator can act on.

    Host and process, because those are the two things somebody reading a
    stuck job needs in order to go and look at it. Not a random token: a
    lease held by ``9f3c1a`` tells nobody which container to inspect.
    """
    return f"{socket.gethostname()}:{os.getpid()}"


def schedule_specs(jobs: Mapping[str, ScheduledJob]) -> tuple[ScheduleSpec, ...]:
    """Translate what the features contributed into what the scheduler stores.

    Two resolutions happen here and both belong at this seam. A job with an
    interval of zero is disabled — that is how every one of them is turned off
    by configuration — so it contributes no schedule at all rather than a row
    the tick has to keep skipping. And a lease of zero means twice the
    interval, which is the right answer for any job with no opinion: a run that
    has not finished by then is stuck rather than slow.
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
    """Wire the queue to this installation's job table.

    ``handlers`` is empty for a process that only publishes. The API enqueues
    and never consumes, and handing it a table it would never use only invites
    something to call it.
    """
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
