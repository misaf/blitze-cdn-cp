"""How durable background work is wired to this installation.

Three decisions live here and nowhere else: which handler runs which kind of
job, what a scheduled job's cadence and lease actually come out as, and who the
worker calls itself. All three are facts about *this* control plane rather than
about queueing, which is why the queue itself knows none of them.

The handler table is the interesting one. The queue resolves a claimed job's
kind through it and learns nothing more, so a convergence and a
plugin-contributed maintenance run share one loop, one lease and one fencing
rule — and adding a third kind is an entry here rather than a change to the
loop.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from blitzecdn.capabilities.jobs.ports import JobHandler, JobStore, ScheduleStore
from blitzecdn.capabilities.jobs.service import (
    DEPLOYMENT_JOB,
    SCHEDULED_JOB,
    JobQueue,
    JobScheduler,
    ScheduleSpec,
)
from blitzecdn.core.plugins import ScheduledJob

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from blitzecdn.composition import ControlPlane

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
    """Translate what the plugins contributed into what the scheduler stores.

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
    platform: ControlPlane,
    *,
    jobs: JobStore,
    handlers: Mapping[str, JobHandler] | None = None,
) -> JobQueue:
    """Wire the queue to this control plane's two kinds of work.

    ``handlers`` is an argument so a process that should not run one of them
    can say so: the API enqueues and never consumes, and handing it a table it
    would never use only invites something to call it.
    """
    return JobQueue(
        jobs=jobs,
        handlers=handlers if handlers is not None else {},
        worker_id=worker_identity(),
        retention=platform.settings.history_retention,
    )


def build_handlers(platform: ControlPlane) -> dict[str, JobHandler]:
    """What each kind of job actually does, in this process.

    Both handlers are one call into a service that was already built for
    duplicate delivery: ``run_queued`` returns the deployment untouched unless
    it is still QUEUED, and a maintenance run is idempotent by construction.
    That is what makes the queue's at-least-once delivery safe to rely on, and
    it is why neither of these does any de-duplication of its own.
    """

    def deployment(payload: Mapping[str, Any]) -> None:
        platform.deployments.run_queued(str(payload["deployment_id"]))

    def scheduled(payload: Mapping[str, Any]) -> None:
        platform.maintenance.run(str(payload["job"]))

    return {DEPLOYMENT_JOB: deployment, SCHEDULED_JOB: scheduled}


def build_job_scheduler(
    platform: ControlPlane, *, schedules: ScheduleStore, queue: JobQueue
) -> JobScheduler:
    """Wire the scheduler to the schedule table and the queue it publishes to."""
    return JobScheduler(schedules=schedules, queue=queue)
