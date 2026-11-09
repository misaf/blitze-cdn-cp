"""How durable background work is wired: cadences, leases and worker identity."""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from dishka import Provider, Scope, provide
from sqlalchemy.exc import SQLAlchemyError

from blitzecdn.capabilities.jobs.adapters.monitor import WorkerMonitor
from blitzecdn.capabilities.jobs.adapters.persistence import JobStore as JobTable
from blitzecdn.capabilities.jobs.adapters.persistence import (
    ScheduleStore as ScheduleTable,
)
from blitzecdn.capabilities.jobs.ports import (
    JobHandler,
    JobStore,
    OperationLeases,
    OperationRuns,
    ScheduleStore,
    WorkerHistory,
    WorkerProgress,
)
from blitzecdn.capabilities.jobs.service import (
    JobQueue,
    JobRunner,
    JobScheduler,
    OperationService,
    ScheduleSpec,
)
from blitzecdn.core.config import Settings
from blitzecdn.core.features import ScheduledJob
from blitzecdn.core.persistence.engine import Database
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.ports.clock import Clock

__all__ = [
    "JobsProvider",
    "QueueReadiness",
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
    clock: Clock,
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
    clock: Clock,
) -> JobScheduler:
    """Wire the scheduler to the schedule table and the queue it publishes to."""
    return JobScheduler(schedules=schedules, queue=queue, uow=uow, clock=clock)


@dataclass(frozen=True, slots=True)
class QueueReadiness:
    """Whether the job table answers; an unmigrated database has no ``jobs``."""

    queue: JobQueue

    def __call__(self) -> bool:
        # A `TypeError` is a bug and is not swallowed.
        try:
            self.queue.list_jobs(limit=1)
        except SQLAlchemyError, OSError:
            return False
        return True


class JobsProvider(Provider):
    """The job tables, the queue over them, its scheduler and the worker loop."""

    scope = Scope.APP

    @provide
    def jobs(self, database: Database) -> JobStore:
        return JobTable(database)

    @provide
    def schedules(self, database: Database) -> ScheduleStore:
        return ScheduleTable(database)

    @provide
    def queue(self, settings: Settings, jobs: JobStore, clock: Clock) -> JobQueue:
        return build_job_queue(
            jobs=jobs, retention=settings.history_retention, clock=clock
        )

    @provide
    def scheduler(
        self, schedules: ScheduleStore, queue: JobQueue, uow: UnitOfWork, clock: Clock
    ) -> JobScheduler:
        return build_job_scheduler(
            schedules=schedules, queue=queue, uow=uow, clock=clock
        )

    @provide
    def readiness(self, queue: JobQueue) -> QueueReadiness:
        return QueueReadiness(queue)

    @provide
    def monitor(self, settings: Settings, clock: Clock) -> Iterator[WorkerMonitor]:
        monitor = WorkerMonitor(settings.state_dir / "workers", clock=clock)
        try:
            yield monitor
        finally:
            monitor.stop()

    @provide
    def worker_history(self, monitor: WorkerMonitor) -> WorkerHistory:
        return monitor

    @provide
    def worker_progress(self, monitor: WorkerMonitor) -> WorkerProgress:
        return monitor

    @provide
    def queued_operations(
        self, queue: JobQueue, runs: OperationRuns
    ) -> OperationService:
        return OperationService(queue=queue, kinds=runs)

    @provide
    def runner(
        self,
        settings: Settings,
        queue: JobQueue,
        scheduler: JobScheduler,
        progress: WorkerProgress,
        clock: Clock,
        operation_leases: OperationLeases,
    ) -> JobRunner:
        return JobRunner(
            queue=queue,
            scheduler=scheduler,
            poll_seconds=settings.worker_poll_seconds,
            clock=clock,
            progress=progress,
            operation_leases=operation_leases,
        )
