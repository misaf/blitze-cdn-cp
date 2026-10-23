"""Leases, fences, retries and crash recovery, against the real database.

These use SQLite rather than a fake store on purpose. Every guarantee in this
capability is a conditional UPDATE — a claim that names the fence it read, a
completion that names the fence it was given — and a fake store would be a
second implementation of exactly the thing under test, free to agree with the
tests and disagree with the database.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event

from blitzecdn.capabilities.jobs.domain import JobStatus, PermanentJobError, backoff_for
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


def test_a_job_that_keeps_killing_its_worker_fails_once_its_attempts_are_spent(
    repository,
):
    """`max_attempts` holds when no handler ever gets to raise.

    A job that takes its worker down — the OOM killer, a SIGKILL after the stop
    grace period — records nothing, so the only evidence is its leases expiring.
    That used to hand it out again on every expiry, `attempts` climbing past
    `max_attempts`, and it stayed `running` without ever being reported.
    """
    clock = Clock()
    queue = queue_for(repository, clock=clock, handlers={})
    job = queue.jobs.enqueue(
        kind="work", payload={}, available_at=_START, max_attempts=2
    )
    for _ in range(2):
        assert queue.claim(lease_seconds=30) is not None
        clock.advance(31)

    assert queue.claim(lease_seconds=30) is None

    abandoned = queue.get(job.id)
    assert abandoned.status is JobStatus.FAILED
    assert abandoned.attempts == 2
    assert abandoned.finished_at == clock()
    assert "abandoned after 2 attempt(s)" in (abandoned.last_error or "")


def test_abandoning_an_exhausted_job_does_not_hold_up_the_next(repository):
    """Retiring one is not a turn of the loop: the claim goes on to real work."""
    clock = Clock()
    record: list[str] = []
    queue = queue_for(repository, clock=clock, handlers={"work": ran(record, "work")})
    stuck = queue.jobs.enqueue(
        kind="work", payload={"id": "stuck"}, available_at=_START, max_attempts=1
    )
    assert queue.claim(lease_seconds=30) is not None
    queue.jobs.enqueue(kind="work", payload={"id": "next"}, available_at=_START)
    clock.advance(31)

    queue.run_one()

    assert record == ["work:next"]
    assert queue.get(stuck.id).status is JobStatus.FAILED


def test_a_paused_worker_cannot_complete_a_job_that_was_abandoned(repository):
    """Abandonment moves the fence, as a claim does.

    The last worker may only have been paused, not killed. Coming back, it must
    not mark as succeeded a job the queue has already reported as failed.
    """
    clock = Clock()
    queue = queue_for(repository, clock=clock, handlers={})
    job = queue.jobs.enqueue(
        kind="work", payload={}, available_at=_START, max_attempts=1
    )
    paused = queue.claim(lease_seconds=30)
    assert paused is not None
    clock.advance(31)
    assert queue.claim() is None

    assert queue.jobs.complete(job.id, paused.fence, now=clock()) is False
    assert queue.get(job.id).status is JobStatus.FAILED


def test_an_abandoned_job_keeps_the_error_an_earlier_attempt_raised(repository):
    clock = Clock()
    attempts: list[int] = []

    def explode_then_vanish(_payload):
        attempts.append(1)
        raise RuntimeError("edge unreachable")

    queue = queue_for(repository, clock=clock, handlers={"work": explode_then_vanish})
    job = queue.jobs.enqueue(
        kind="work", payload={}, available_at=_START, max_attempts=2
    )
    queue.run_one()
    clock.advance(backoff_for(1).total_seconds())
    assert queue.claim(lease_seconds=30) is not None  # ... and the worker dies
    clock.advance(31)

    assert queue.claim() is None

    error = queue.get(job.id).last_error or ""
    assert "abandoned after 2 attempt(s)" in error
    assert "RuntimeError: edge unreachable" in error


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


# -- Two processes on one file ---------------------------------------------


def test_an_enqueue_landing_mid_claim_waits_instead_of_failing_the_claim(settings):
    """The API and the worker are two processes writing one SQLite file.

    A claim reads its row and then writes it. In a deferred transaction an
    enqueue the API commits between the two turns the worker's write into a
    snapshot-busy error that no `busy_timeout` waits out: the fence kept that
    safe, but the claim failed and the worker lost a poll. Under BEGIN
    IMMEDIATE the claim holds the writer from its first read, and the enqueue
    waits its turn.

    Two repositories stand in for the two processes — two engines, two
    connections — and the enqueue is fired from the worker's own UPDATE, which
    is the one moment it can do harm.
    """
    worker_side = Repository(settings.database_path)
    api_side = Repository(settings.database_path)
    queue = queue_for(worker_side, handlers={})
    queue.jobs.enqueue(kind="work", payload={}, available_at=_START)
    racing: list[threading.Thread] = []

    def enqueue_from_the_api():
        api_side.jobs.enqueue(kind="work", payload={}, available_at=_START)

    def between_read_and_write(_conn, _cursor, statement, *_args):
        if racing or not statement.lstrip().upper().startswith("UPDATE JOBS"):
            return
        racing.append(threading.Thread(target=enqueue_from_the_api))
        racing[0].start()
        # Long enough for an unobstructed enqueue to commit, which is what it
        # did before; under BEGIN IMMEDIATE it is still waiting when this ends.
        racing[0].join(timeout=1.0)

    event.listen(
        worker_side.database.engine, "before_cursor_execute", between_read_and_write
    )
    try:
        claim = queue.claim()
    finally:
        event.remove(
            worker_side.database.engine,
            "before_cursor_execute",
            between_read_and_write,
        )
    racing[0].join(timeout=10)

    assert claim is not None
    assert not racing[0].is_alive()
    assert len(worker_side.jobs.list_jobs()) == 2


def test_a_handler_can_say_a_retry_would_meet_the_same_answer(repository):
    """`PermanentJobError` fails at once, attempts left or not.

    The other permanent case beside a missing handler, and the one a handler
    decides for itself: a deployment that was abandoned is abandoned on every
    attempt, so backing off and asking again would only repeat the answer.
    """
    attempts: list[int] = []

    def abandoned(_payload):
        attempts.append(1)
        raise PermanentJobError("deployment dep-1 was abandoned")

    queue = queue_for(repository, handlers={"work": abandoned})
    job = queue.jobs.enqueue(
        kind="work", payload={}, available_at=_START, max_attempts=3
    )

    queue.run_one()

    failed = queue.get(job.id)
    assert attempts == [1]
    assert failed.status is JobStatus.FAILED
    assert failed.last_error == "deployment dep-1 was abandoned"


def test_a_refused_duplicate_inside_a_transaction_leaves_it_usable(repository):
    """The dedupe refusal undoes only itself.

    A deployment is now submitted with its job in one transaction, so the
    insert this index refuses can be one statement in somebody else's unit of
    work. Caught without a savepoint, the failed flush poisoned the session and
    the caller's commit failed with it.
    """
    with repository.transaction():
        first = repository.jobs.enqueue(
            kind="work", payload={}, available_at=_START, dedupe_key="only-one"
        )
        refused = repository.jobs.enqueue(
            kind="work", payload={}, available_at=_START, dedupe_key="only-one"
        )
        other = repository.jobs.enqueue(kind="other", payload={}, available_at=_START)

    assert first is not None
    assert refused is None
    assert other is not None
    assert {job.kind for job in repository.jobs.list_jobs()} == {"work", "other"}
