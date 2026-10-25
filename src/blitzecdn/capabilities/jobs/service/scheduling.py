"""Turning intervals into queued jobs, exactly once per firing.

A function of the clock and a table; each firing advances its schedule by
compare-and-swap, so two controllers enqueue once between them.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime

from blitzecdn.capabilities.jobs.domain import Schedule, next_due
from blitzecdn.capabilities.jobs.ports import ScheduleStore
from blitzecdn.capabilities.jobs.service.queue import JobQueue
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.ports.clock import Clock, system_clock

__all__ = ["JobScheduler", "ScheduleSpec"]

_LOGGER = logging.getLogger(__name__)


class ScheduleSpec:
    """A scheduled job's cadence and lease, without the feature machinery."""

    __slots__ = ("interval_seconds", "jitter_seconds", "lease_seconds", "name")

    def __init__(
        self,
        name: str,
        *,
        interval_seconds: int,
        lease_seconds: int,
        jitter_seconds: int = 0,
    ) -> None:
        self.name = name
        self.interval_seconds = interval_seconds
        self.lease_seconds = lease_seconds
        self.jitter_seconds = jitter_seconds


class JobScheduler:
    """Registers recurring work, and enqueues whatever is due."""

    def __init__(
        self,
        *,
        schedules: ScheduleStore,
        queue: JobQueue,
        uow: UnitOfWork,
        clock: Clock = system_clock,
    ) -> None:
        self.schedules = schedules
        self.queue = queue
        self.uow = uow
        self.clock = clock

    def register(self, specs: Sequence[ScheduleSpec]) -> Sequence[Schedule]:
        """Make the table match this installation's contributed schedules.

        Idempotent and preserving ``next_run_at``; uncontributed schedules are removed.
        A new schedule is first due one interval from now, not at startup.
        """
        now = self.clock()
        registered = [
            self.schedules.upsert(
                Schedule(
                    name=spec.name,
                    interval_seconds=spec.interval_seconds,
                    jitter_seconds=spec.jitter_seconds,
                    lease_seconds=spec.lease_seconds,
                    next_run_at=next_due(
                        now=now,
                        interval_seconds=spec.interval_seconds,
                        jitter_seconds=spec.jitter_seconds,
                    ),
                )
            )
            for spec in specs
        ]
        self.schedules.forget([spec.name for spec in specs])
        return registered

    def tick(self, *, now: datetime | None = None) -> Sequence[str]:
        """Enqueue every due schedule, each with its advance in one transaction.

        A failed enqueue leaves it due; an in-flight job skips this interval.
        """
        moment = now or self.clock()
        fired: list[str] = []
        for schedule in self.schedules.due(moment):
            with self.uow.transaction():
                won = self.schedules.reschedule(
                    schedule.name,
                    expected=schedule.next_run_at,
                    next_run_at=next_due(
                        now=moment,
                        interval_seconds=schedule.interval_seconds,
                        jitter_seconds=schedule.jitter_seconds,
                    ),
                    ran_at=moment,
                )
                if not won:
                    continue
                if self.queue.enqueue_scheduled(schedule.name) is None:
                    _LOGGER.info(
                        "scheduled job %r is still in flight; skipping this firing",
                        schedule.name,
                    )
                    continue
                fired.append(schedule.name)
        return fired

    def leases(self) -> Mapping[str, int]:
        """How long each scheduled job's claim should be good for."""
        return {
            schedule.name: schedule.lease_seconds
            for schedule in self.schedules.list_schedules()
        }
