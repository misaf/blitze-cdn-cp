"""Leases, fences, retries and crash recovery, against the real database.

These use SQLite rather than a fake store on purpose. Every guarantee in this
capability is a conditional UPDATE — a claim that names the fence it read, a
completion that names the fence it was given — and a fake store would be a
second implementation of exactly the thing under test, free to agree with the
tests and disagree with the database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from blitzecdn.capabilities.jobs.domain import JobStatus, backoff_for
from blitzecdn.capabilities.jobs.service import JobQueue
from blitzecdn.composition import Repository

_START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class Clock:
    """A hand-wound clock, so a lease can expire without anybody sleeping."""

    def __init__(self, now: datetime = _START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def repository(settings):
    return Repository(settings.database_path)


def queue_for(
    repository, *, worker: str = "worker-a", clock=None, **kwargs
) -> JobQueue:
    return JobQueue(
        jobs=repository.jobs,
        worker_id=worker,
        clock=clock or Clock(),
        **kwargs,
    )


def ran(record: list[str], name: str):
    def handler(payload):
        record.append(f"{name}:{payload.get('id', '')}")

    return handler


# -- Delivery --------------------------------------------------------------


def test_a_queued_job_is_claimed_and_run(repository):
    record: list[str] = []
    queue = queue_for(repository, handlers={"work": ran(record, "work")})
    job = queue.jobs.enqueue(kind="work", payload={"id": "1"}, available_at=_START)

    queue.run_one()

    assert record == ["work:1"]
    assert queue.get(job.id).status is JobStatus.SUCCEEDED


def test_a_job_is_not_claimed_before_it_is_available(repository):
    clock = Clock()
    queue = queue_for(repository, clock=clock, handlers={"work": lambda _p: None})
    queue.jobs.enqueue(
        kind="work", payload={}, available_at=_START + timedelta(seconds=30)
    )

    assert queue.run_one() is None

    clock.advance(31)
    assert queue.run_one() is not None


def test_an_empty_queue_claims_nothing(repository):
    assert queue_for(repository, handlers={}).run_one() is None


# -- Single flight ---------------------------------------------------------


def test_a_second_copy_of_a_scheduled_job_is_refused_while_one_is_in_flight(
    repository,
):
    """What the Redis `SET NX` key did, as a partial unique index.

    Unlike that key it cannot outlive the work it guards: it *is* a column on
    the job, so a worker that dies cannot leave a lock behind that keeps the
    next firing out of the queue for its whole TTL.
    """
    queue = queue_for(repository, handlers={})

    assert queue.enqueue_scheduled("renew-certificates") is not None
    assert queue.enqueue_scheduled("renew-certificates") is None


def test_the_next_firing_is_admitted_once_the_last_one_finished(repository):
    record: list[str] = []
    queue = queue_for(repository, handlers={"scheduled": ran(record, "run")})
    queue.enqueue_scheduled("renew-certificates")
    queue.run_one()

    assert queue.enqueue_scheduled("renew-certificates") is not None


def test_a_deployment_is_deduplicated_on_its_identifier(repository):
    queue = queue_for(repository, handlers={})

    assert queue.enqueue_deployment("d1") is not None
    assert queue.enqueue_deployment("d1") is None
    assert queue.enqueue_deployment("d2") is not None


# -- Duplicate execution ---------------------------------------------------


def test_a_job_delivered_twice_runs_its_handler_twice_and_that_is_declared(
    repository,
):
    """At-least-once, stated as a test rather than left to be discovered.

    Exactly-once is not on offer here and could not honestly be: the handler's
    side effects are outside this database's transaction. What makes it safe is
    that both handlers were built for it — `run_queued` returns untouched
    unless the deployment is still QUEUED, and a maintenance run is idempotent.
    """
    clock = Clock()
    calls: list[int] = []
    queue = queue_for(
        repository, clock=clock, handlers={"work": lambda _p: calls.append(1)}
    )
    queue.jobs.enqueue(kind="work", payload={}, available_at=_START)

    claim = queue.claim(lease_seconds=10)
    assert claim is not None
    # The first worker is still inside the handler when its lease runs out.
    clock.advance(11)
    second = queue_for(
        repository,
        worker="worker-b",
        clock=clock,
        handlers={"work": lambda _p: calls.append(2)},
    )
    second.run_one()

    assert calls == [2]
    # And the first worker, coming back, cannot record a result for a run that
    # is no longer its own.
    assert queue.jobs.complete(claim.job.id, claim.fence, now=clock()) is False


# -- Stale workers ---------------------------------------------------------


def test_a_worker_whose_lease_expired_cannot_complete_its_job(repository):
    """The fence, which is what a lease alone does not give you.

    A worker paused past its lease — a long stop-the-world pause, a suspended
    container — comes back holding a token that no longer matches, so it cannot
    mark as succeeded a job another worker has since re-run.
    """
    clock = Clock()
    queue = queue_for(repository, clock=clock, handlers={})
    queue.jobs.enqueue(kind="work", payload={}, available_at=_START)
    stale = queue.claim(lease_seconds=5)
    assert stale is not None

    clock.advance(6)
    fresh = queue_for(repository, worker="worker-b", clock=clock, handlers={}).claim()
    assert fresh is not None
    assert fresh.fence > stale.fence

    assert queue.jobs.complete(stale.job.id, stale.fence, now=clock()) is False
    assert queue.jobs.renew(stale.job.id, stale.fence, until=clock()) is False
    assert queue.jobs.complete(fresh.job.id, fresh.fence, now=clock()) is True


def test_two_workers_claiming_at_once_produce_one_owner(repository):
    """The compare-and-swap on the fence, exercised through two queues.

    Both may select the same row; the second's UPDATE names a fence that has
    already moved and matches nothing.
    """
    queue = queue_for(repository, handlers={})
    queue.jobs.enqueue(kind="work", payload={}, available_at=_START)

    first = queue.claim()
    second = queue_for(repository, worker="worker-b", handlers={}).claim()

    assert first is not None
    assert second is None


def test_a_renewed_lease_keeps_the_job_out_of_another_workers_reach(repository):
    clock = Clock()
    queue = queue_for(repository, clock=clock, handlers={})
    queue.jobs.enqueue(kind="work", payload={}, available_at=_START)
    claim = queue.claim(lease_seconds=10)
    assert claim is not None

    clock.advance(8)
    assert queue.renew(claim, seconds=60) is True
    clock.advance(8)

    assert (
        queue_for(repository, worker="worker-b", clock=clock, handlers={}).claim()
        is None
    )


# -- Crash recovery --------------------------------------------------------


def test_a_job_a_dead_worker_was_holding_becomes_claimable_again(repository):
    """No sweep, no startup pass: the expired lease *is* the recovery.

    A worker that dies simply stops renewing, and nothing has to observe the
    death for the work to become claimable.
    """
    clock = Clock()
    record: list[str] = []
    abandoned = queue_for(repository, clock=clock, handlers={})
    abandoned.jobs.enqueue(kind="work", payload={"id": "1"}, available_at=_START)
    assert abandoned.claim(lease_seconds=30) is not None

    clock.advance(31)
    survivor = queue_for(
        repository,
        worker="worker-b",
        clock=clock,
        handlers={"work": ran(record, "work")},
    )
    survivor.run_one()

    assert record == ["work:1"]


# -- Failure and retry -----------------------------------------------------


def test_a_failing_job_retries_with_bounded_backoff(repository):
    clock = Clock()
    attempts: list[int] = []

    def explode(_payload):
        attempts.append(1)
        raise RuntimeError("edge unreachable")

    queue = queue_for(repository, clock=clock, handlers={"work": explode})
    job = queue.jobs.enqueue(
        kind="work", payload={}, available_at=_START, max_attempts=3
    )

    queue.run_one()
    after = queue.get(job.id)
    assert after.status is JobStatus.PENDING
    assert after.available_at == _START + backoff_for(1)
    assert "edge unreachable" in (after.last_error or "")

    clock.advance(backoff_for(1).total_seconds())
    queue.run_one()
    clock.advance(backoff_for(2).total_seconds())
    queue.run_one()

    assert attempts == [1, 1, 1]
    exhausted = queue.get(job.id)
    assert exhausted.status is JobStatus.FAILED
    assert exhausted.finished_at is not None


def test_a_job_whose_kind_has_no_handler_fails_permanently(repository):
    """A plugin uninstalled while one of its jobs was still queued.

    Retrying would produce the same missing handler at every attempt, so this
    is one of the two failures that skip the backoff entirely.
    """
    queue = queue_for(repository, handlers={})
    job = queue.jobs.enqueue(kind="waf-refresh", payload={}, available_at=_START)

    queue.run_one()

    failed = queue.get(job.id)
    assert failed.status is JobStatus.FAILED
    assert "waf-refresh" in (failed.last_error or "")


# -- Retention -------------------------------------------------------------


def test_pruning_removes_finished_jobs_and_never_pending_ones(repository):
    """A pending job is work nobody has done. Losing one loses a deployment."""
    queue = queue_for(repository, handlers={"work": lambda _p: None}, retention=1)
    for index in range(3):
        queue.jobs.enqueue(kind="work", payload={"id": str(index)}, available_at=_START)
        queue.run_one()
    waiting = queue.jobs.enqueue(kind="work", payload={}, available_at=_START)

    removed = queue.prune()

    assert removed == 2
    assert queue.get(waiting.id).status is JobStatus.PENDING
