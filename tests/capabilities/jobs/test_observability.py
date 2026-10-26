"""Queue aggregates and worker pulses across process boundaries."""

import json
import logging
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from blitzecdn.capabilities.jobs.adapters import monitor as monitor_module
from blitzecdn.capabilities.jobs.adapters.monitor import WorkerMonitor
from blitzecdn.capabilities.jobs.domain import DONE, JobStatus
from blitzecdn.capabilities.jobs.service import JobQueue, JobRunner
from blitzecdn.composition import Repository


def test_queue_aggregates_cover_all_rows_and_exclude_future_jobs(settings):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    repository = Repository(settings.database_path, clock=lambda: now)
    with repository.transaction():
        for index in range(150):
            repository.jobs.enqueue(
                kind="example",
                payload={"private": "payload"},
                available_at=now - timedelta(seconds=index + 1),
            )
        repository.jobs.enqueue(
            kind="later",
            payload={},
            available_at=now + timedelta(days=1),
        )
    # The oldest ready row is now running, then its lease expires.
    claim = repository.jobs.claim(worker="worker-a", now=now, lease_seconds=5)
    assert claim is not None
    report = repository.jobs.statistics(now=now + timedelta(seconds=6))
    assert report.counts[JobStatus.PENDING] == 150
    assert report.counts[JobStatus.RUNNING] == 1
    assert report.ready == 149
    assert report.expired_leases == 1
    assert report.oldest_ready_age_seconds == 155
    assert "payload" not in report.model_dump_json()
    repository.close()


def test_heartbeat_advances_while_handler_is_blocked(settings, tmp_path, monkeypatch):
    repository = Repository(settings.database_path)
    entered, release, pulsed = threading.Event(), threading.Event(), threading.Event()
    observed = []
    write = monitor_module.atomic_write_bytes

    def record(path, payload):
        write(path, payload)
        document = json.loads(payload)
        if document["current_job_id"] is not None:
            observed.append(document["heartbeat_at"])
            if len(observed) >= 2:
                pulsed.set()

    monkeypatch.setattr(monitor_module, "atomic_write_bytes", record)

    def handler(_payload):
        entered.set()
        assert release.wait(5), "test did not release its disposable handler"
        return DONE

    monitor = WorkerMonitor(tmp_path / "workers", interval=0.01)
    queue = JobQueue(
        jobs=repository.jobs, handlers={"example": handler}, worker_id="host:1"
    )
    job = repository.jobs.enqueue(
        kind="example", payload={}, available_at=queue.clock()
    )
    runner = JobRunner(
        queue=queue,
        scheduler=SimpleNamespace(tick=lambda **_: []),
        progress=monitor,
    )
    runner.start_reporting()
    thread = threading.Thread(target=runner.tick)
    thread.start()
    try:
        assert entered.wait(5)
        assert pulsed.wait(5)
        (worker,) = WorkerMonitor(tmp_path / "workers").read().workers
        assert worker.worker_id == "host:1"
        assert worker.current_job_id == job.id
        assert worker.current_kind == "example"
        assert observed[-1] > observed[0]
        assert (
            next((tmp_path / "workers").glob("*.json")).stat().st_mode & 0o777 == 0o600
        )
    finally:
        release.set()
        thread.join(timeout=5)
        try:
            (worker,) = monitor.read().workers
            assert worker.current_job_id is None
            assert worker.last_duration_seconds >= 0
            assert queue.get(job.id).status is JobStatus.SUCCEEDED
        finally:
            runner.stop_reporting()
            repository.close()
    assert monitor.read().workers == ()


def test_bad_worker_records_are_reported_without_following_symlinks(tmp_path):
    (tmp_path / "broken.json").write_text("not JSON")
    (tmp_path / "link.json").symlink_to(tmp_path / "broken.json")
    report = WorkerMonitor(tmp_path).read()
    assert report.workers == ()
    assert report.unreadable == 2


def test_reporting_failure_does_not_stop_the_worker(tmp_path, monkeypatch, caplog):
    def unavailable(*_args, **_kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(monitor_module, "atomic_write_bytes", unavailable)
    monitor = WorkerMonitor(tmp_path)
    try:
        monitor.start("worker-a")
        assert "could not publish worker heartbeat" in caplog.text
    finally:
        monitor.stop()


@pytest.mark.parametrize("status", ["failed", "succeeded"])
def test_completed_jobs_remain_in_retained_counts(settings, status):
    repository = Repository(settings.database_path)
    now = repository.database.now()
    repository.jobs.enqueue(kind="example", payload={}, available_at=now)
    claim = repository.jobs.claim(worker="worker-a", now=now, lease_seconds=5)
    if status == "succeeded":
        repository.jobs.complete(claim.job.id, claim.fence, now=now)
    else:
        repository.jobs.fail(
            claim.job.id, claim.fence, now=now, error="failed", retry_at=None
        )
    assert repository.jobs.statistics(now=now).counts[JobStatus(status)] == 1
    repository.close()


def test_job_logs_have_correlation_and_duration_without_payloads(settings, caplog):
    repository = Repository(settings.database_path)
    queue = JobQueue(
        jobs=repository.jobs,
        handlers={"example": lambda _: DONE},
        worker_id="worker-a",
    )
    job = repository.jobs.enqueue(
        kind="example",
        payload={"credential": "private-payload-value"},
        available_at=queue.clock(),
    )
    with caplog.at_level(logging.INFO):
        queue.run_one()
    started, finished = [
        record for record in caplog.records if record.message.startswith("job handler")
    ]
    assert started.job_id == finished.job_id == job.id
    assert finished.worker_id == "worker-a"
    assert finished.job_kind == "example"
    assert finished.attempt == 1
    assert finished.duration_seconds >= 0
    assert "private-payload-value" not in caplog.text
    repository.close()
