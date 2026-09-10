"""Turning "every four hours" into rows, durably and exactly once per firing.

The scheduler is not a timer. It is a function of the clock and a table: ask it
to tick, and it enqueues every schedule whose time has come, moving each one
forward with a compare-and-swap so that two controllers ticking at the same
instant produce one enqueue between them rather than two.

That property is why the table exists at all. The APScheduler timer this
replaces kept the schedule in memory, which had one failure mode that only
shows on a bad day: a controller that was down over the hour a renewal was due
came back with the timer reset and never ran that firing — the job's next
chance was a whole interval later, and nothing recorded that one had been
missed. A due row is still due after a restart.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from blitzecdn.capabilities.jobs.domain import Schedule, next_due, utcnow
from blitzecdn.capabilities.jobs.ports import ScheduleStore
from blitzecdn.capabilities.jobs.service.queue import JobQueue

__all__ = ["JobScheduler", "ScheduleSpec"]

_LOGGER = logging.getLogger(__name__)


class ScheduleSpec:
    """What a plugin's scheduled job says about its own cadence.

    A small value rather than `core.plugins.ScheduledJob` itself, because this
    capability must not import the plugin machinery to know how often to do
    something. The composition root translates one into the other, which is
    also where "a lease of zero means twice the interval" is resolved — a
    default belongs where the two vocabularies meet, not in the registry and
    not in the store.
    """

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
        clock: Any = utcnow,
    ) -> None:
        self.schedules = schedules
        self.queue = queue
        self.clock = clock

    def register(self, specs: Sequence[ScheduleSpec]) -> Sequence[Schedule]:
        """Make the table say what this installation's plugins contribute.

        Idempotent, because it runs on every process start. An existing
        schedule keeps its ``next_run_at`` — a controller restarted every few
        minutes, which is what a crash loop or a rolling upgrade looks like,
        must not push every schedule permanently into the future — and a
        schedule no installed plugin contributes any more is removed, so an
        uninstalled package stops producing one "no such job" per interval.

        A schedule seen for the first time is due one interval from now rather
        than immediately. Starting a process must not be a way to trigger every
        piece of maintenance at once, and on a fresh installation there is
        nothing for any of them to do yet.
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
        """Enqueue every schedule that is due, and say which ones those were.

        The order is: move the schedule forward, *then* enqueue. Doing it the
        other way round would leave a controller that died between the two
        steps with a schedule still due and a job already queued, and the next
        tick would queue a second one. This way the loser of that race is a
        firing that was recorded as having happened and did not — recoverable
        by the next interval, and far cheaper than a duplicate convergence.

        A duplicate is refused anyway: ``enqueue_scheduled`` is keyed on the
        job name and the partial unique index binds while the last copy is
        unfinished. The ordering is the belt to that index's braces.
        """
        moment = now or self.clock()
        fired: list[str] = []
        for schedule in self.schedules.due(moment):
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
                # Another controller moved this schedule between the read and
                # the write. It owns this firing; there is nothing to report.
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
