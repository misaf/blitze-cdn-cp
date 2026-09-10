"""Recurring work that survives a restart, and fires once however many ask.

The scheduler is a function of a clock and a table, so none of this needs a
process, a timer or a sleep. That is the property being demonstrated as much as
any assertion: the thing this replaced kept its schedule in memory, and the one
failure it had could only be reproduced by restarting a process.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from blitzecdn.capabilities.jobs.composition import schedule_specs
from blitzecdn.capabilities.jobs.service import JobQueue, JobScheduler, ScheduleSpec
from blitzecdn.composition import Repository
from blitzecdn.core.plugins import ScheduledJob

_START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = _START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def repository(settings):
    return Repository(settings.database_path)


def scheduler_for(repository, clock, *, handlers=None) -> tuple[JobScheduler, JobQueue]:
    queue = JobQueue(
        jobs=repository.jobs,
        handlers=handlers or {},
        worker_id="worker-a",
        clock=clock,
    )
    return JobScheduler(schedules=repository.schedules, queue=queue, clock=clock), queue


HOURLY = ScheduleSpec("renew-certificates", interval_seconds=3600, lease_seconds=7200)


def test_a_new_schedule_is_due_one_interval_from_now_not_immediately(repository):
    """Starting a process must not trigger every piece of maintenance at once.

    On a fresh installation there is nothing for any of them to do, and on an
    existing one a restart would mean a certificate sweep and a drift check
    racing the operator who just restarted it.
    """
    clock = Clock()
    scheduler, _ = scheduler_for(repository, clock)

    scheduler.register([HOURLY])

    assert scheduler.tick() == []
    clock.advance(3600)
    assert scheduler.tick() == ["renew-certificates"]


def test_registering_again_does_not_move_an_existing_schedule(repository):
    """A crash loop or a rolling upgrade restarts this process repeatedly.

    Writing `next_run_at` on every registration would push every schedule
    permanently into the future and run nothing at all.
    """
    clock = Clock()
    scheduler, _ = scheduler_for(repository, clock)
    scheduler.register([HOURLY])

    clock.advance(3000)
    scheduler.register([HOURLY])
    clock.advance(600)

    assert scheduler.tick() == ["renew-certificates"]


def test_a_schedule_that_came_due_during_an_outage_still_fires(repository):
    """The failure the in-memory timer had, and the reason the table exists.

    A controller down over the hour a renewal was due used to come back with
    its timer reset and simply never run that firing.
    """
    clock = Clock()
    scheduler, _ = scheduler_for(repository, clock)
    scheduler.register([HOURLY])

    # The process is gone for six hours. A fresh one reads the same table.
    clock.advance(6 * 3600)
    restarted, _ = scheduler_for(repository, clock)
    restarted.register([HOURLY])

    assert restarted.tick() == ["renew-certificates"]


def test_an_overdue_schedule_fires_once_rather_than_once_per_missed_interval(
    repository,
):
    """Nothing here benefits from catching up.

    The work is "renew what is expiring", not "produce a report for each hour",
    so six missed firings are one thing to do rather than six.
    """
    clock = Clock()
    scheduler, queue = scheduler_for(repository, clock)
    scheduler.register([HOURLY])
    clock.advance(6 * 3600)

    scheduler.tick()

    assert len(queue.list_jobs()) == 1
    assert scheduler.tick() == []


def test_two_controllers_ticking_at_once_produce_one_firing(repository):
    """The compare-and-swap on `next_run_at`, which is what makes two safe.

    Both see the schedule as due; exactly one wins the right to move it, and
    only that one goes on to enqueue.
    """
    clock = Clock()
    first, queue = scheduler_for(repository, clock)
    second, _ = scheduler_for(repository, clock)
    first.register([HOURLY])
    clock.advance(3600)

    fired = first.tick() + second.tick()

    assert fired == ["renew-certificates"]
    assert len(queue.list_jobs()) == 1


def test_a_firing_is_skipped_while_the_last_one_is_still_running(repository):
    """A slow job must not stack copies of itself behind the schedule."""
    clock = Clock()
    scheduler, queue = scheduler_for(repository, clock)
    scheduler.register([HOURLY])
    clock.advance(3600)
    scheduler.tick()
    queue.claim()  # still running; nothing has completed it

    clock.advance(3600)
    assert scheduler.tick() == []


def test_a_disabled_job_contributes_no_schedule():
    """An interval of zero is how every one of them is turned off.

    Resolved where the two vocabularies meet — a plugin's `ScheduledJob` and
    this capability's `ScheduleSpec` — rather than as a row the tick has to
    keep skipping.
    """
    jobs = {
        "drift": ScheduledJob(
            plugin="deployments", name="drift", interval_seconds=0, run=lambda _o: None
        ),
        "renew": ScheduledJob(
            plugin="tls", name="renew", interval_seconds=3600, run=lambda _o: None
        ),
    }

    assert [spec.name for spec in schedule_specs(jobs)] == ["renew"]


def test_a_job_with_no_lease_opinion_gets_twice_its_interval():
    """A run that has not finished by then is stuck rather than slow."""
    jobs = {
        "renew": ScheduledJob(
            plugin="tls", name="renew", interval_seconds=3600, run=lambda _o: None
        )
    }

    (spec,) = schedule_specs(jobs)

    assert spec.lease_seconds == 7200


def test_an_uninstalled_plugins_schedule_is_forgotten(repository):
    """Otherwise it enqueues forever and resolves to nothing.

    One `NotFoundError` per interval, about a package the operator removed on
    purpose.
    """
    clock = Clock()
    scheduler, _ = scheduler_for(repository, clock)
    scheduler.register(
        [HOURLY, ScheduleSpec("waf-refresh", interval_seconds=600, lease_seconds=1200)]
    )

    scheduler.register([HOURLY])

    assert [item.name for item in repository.schedules.list_schedules()] == [
        "renew-certificates"
    ]


def test_the_lease_a_schedule_declares_is_what_is_stored(repository):
    clock = Clock()
    scheduler, _ = scheduler_for(repository, clock)
    scheduler.register([HOURLY])

    assert scheduler.leases() == {"renew-certificates": 7200}
