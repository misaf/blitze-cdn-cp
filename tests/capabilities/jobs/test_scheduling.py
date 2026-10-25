"""Recurring work that survives a restart, and fires once however many ask.

The scheduler is a function of a clock and a table, so none of this needs a
process, a timer or a sleep. That is the property being demonstrated as much as
any assertion: the thing this replaced kept its schedule in memory, and the one
failure it had could only be reproduced by restarting a process.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from blitzecdn.capabilities.jobs.composition import schedule_specs
from blitzecdn.capabilities.jobs.domain import DONE, JobStatus
from blitzecdn.capabilities.jobs.service import (
    DEPLOYMENT_JOB,
    SCHEDULED_JOB,
    JobQueue,
    JobRunner,
    JobScheduler,
    ScheduleSpec,
)
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
    return JobScheduler(
        schedules=repository.schedules, queue=queue, uow=repository, clock=clock
    ), queue


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


@pytest.mark.parametrize("after_insert", [False, True])
@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_failed_publication_leaves_the_schedule_due_after_restart(
    repository, settings, monkeypatch, after_insert, failure
):
    clock = Clock()
    scheduler, queue = scheduler_for(repository, clock)
    scheduler.register([HOURLY])
    clock.advance(3600)
    before = repository.schedules.list_schedules()
    enqueue = queue.enqueue_scheduled

    def fail(name):
        if after_insert:
            enqueue(name)
        raise failure("publication interrupted")

    monkeypatch.setattr(queue, "enqueue_scheduled", fail)
    with pytest.raises(failure, match="publication interrupted"):
        scheduler.tick()

    assert repository.schedules.list_schedules() == before
    assert queue.list_jobs() == []
    restarted = Repository(settings.database_path)
    try:
        fresh, jobs = scheduler_for(restarted, clock)
        assert fresh.tick() == [HOURLY.name]
        (job,) = jobs.list_jobs()
        assert job.payload == {"job": HOURLY.name}
        assert fresh.tick() == []
        assert len(jobs.list_jobs()) == 1
    finally:
        restarted.close()


def test_concurrent_schedulers_publish_one_job_from_the_same_due_snapshot(
    repository, settings, monkeypatch
):
    clock = Clock()
    first, queue = scheduler_for(repository, clock)
    first.register([HOURLY])
    clock.advance(3600)
    other = Repository(settings.database_path)
    second, _ = scheduler_for(other, clock)
    both_due = threading.Barrier(2)

    def observe(store):
        due = store.due

        def read(now):
            schedules = due(now)
            both_due.wait(timeout=10)
            return schedules

        monkeypatch.setattr(store, "due", read)

    observe(repository.schedules)
    observe(other.schedules)
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = [workers.submit(scheduler.tick) for scheduler in (first, second)]
            fired = [name for result in results for name in result.result(timeout=10)]
        assert fired == [HOURLY.name]
        assert len(queue.list_jobs()) == 1
    finally:
        other.close()


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


def test_a_scheduled_job_runs_under_the_lease_its_schedule_declares(repository):
    """Stored, and now read: the claim is held for the schedule's two hours.

    A claim's lease is fixed before anybody knows which job it returns, so a
    scheduled job used to run under the queue's 900-second default whatever its
    schedule said — and a worker that died an hour into a two-hour renewal
    sweep would have had the sweep taken over while it might still be running.
    """
    clock = Clock()
    leases: list[datetime | None] = []

    def observe(_payload):
        (job,) = repository.jobs.list_jobs()
        leases.append(job.leased_until)
        return DONE

    scheduler, queue = scheduler_for(
        repository, clock, handlers={SCHEDULED_JOB: observe}
    )
    scheduler.register([HOURLY])
    clock.advance(3600)

    assert JobRunner(queue=queue, scheduler=scheduler, clock=clock).tick() is True

    assert leases == [clock() + timedelta(seconds=7200)]


def test_a_deployment_keeps_the_queue_s_own_lease(repository):
    """Only a scheduled job has a schedule to ask."""
    clock = Clock()
    leases: list[datetime | None] = []

    def observe(_payload):
        (job,) = repository.jobs.list_jobs()
        leases.append(job.leased_until)
        return DONE

    scheduler, queue = scheduler_for(
        repository, clock, handlers={DEPLOYMENT_JOB: observe}
    )
    scheduler.register([HOURLY])
    queue.enqueue_deployment("dep-1")

    JobRunner(queue=queue, scheduler=scheduler, clock=clock).tick()

    assert leases == [clock() + timedelta(seconds=queue.lease_seconds)]


@pytest.mark.parametrize("succeeds", [False, True])
def test_worker_bounds_finished_job_history_and_preserves_live_work(
    repository, succeeds
):
    clock = Clock()
    scheduler, queue = scheduler_for(
        repository, clock, handlers={"work": lambda _payload: DONE} if succeeds else {}
    )
    queue.retention = 1
    running = repository.jobs.enqueue(kind="work", payload={}, available_at=clock())
    assert queue.claim() is not None
    pending = repository.jobs.enqueue(
        kind="work", payload={}, available_at=clock() + timedelta(hours=1)
    )
    for _ in range(3):
        repository.jobs.enqueue(kind="work", payload={}, available_at=clock())
        clock.advance(1)
    runner = JobRunner(queue=queue, scheduler=scheduler, clock=clock)

    for _ in range(3):
        assert runner.tick() is True

    remaining = repository.jobs.list_jobs()
    assert len(remaining) == 3
    assert queue.get(running.id).status is JobStatus.RUNNING
    assert queue.get(pending.id).status is JobStatus.PENDING
    terminal = JobStatus.SUCCEEDED if succeeds else JobStatus.FAILED
    assert len([job for job in remaining if job.status is terminal]) == 1
