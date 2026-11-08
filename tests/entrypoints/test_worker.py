"""The worker process entry point: startup, the signal-driven stop, and teardown.

These run `run_worker` itself in the test's main thread, so its signal
handlers are the real ones and a SIGTERM is a real signal. The handlers are
put back afterwards, or the rest of the run would inherit them.
"""

from __future__ import annotations

import signal
from datetime import UTC, datetime, timedelta

import pytest

import blitzecdn.composition as composition
from blitzecdn.capabilities.jobs.domain import DONE, JobStatus
from blitzecdn.composition import Repository, Worker
from blitzecdn.core.config import Settings
from blitzecdn.entrypoints.worker import run_worker

_STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)


@pytest.fixture(autouse=True)
def restore_signal_handlers():
    saved = {received: signal.getsignal(received) for received in _STOP_SIGNALS}
    yield saved
    for received, handler in saved.items():
        signal.signal(received, handler)


class _RecordingContainer:
    """The real container, with `close` observed: the only other call is `get`."""

    def __init__(self, container) -> None:
        self._container = container
        self.closed = False

    def get(self, dependency):
        return self._container.get(dependency)

    def close(self) -> None:
        self.closed = True
        self._container.close()


@pytest.fixture
def stops(monkeypatch):
    """Every `Worker.stop`, still doing the real teardown."""
    calls: list[Worker] = []
    real_stop = Worker.stop

    def stop(self: Worker) -> None:
        calls.append(self)
        real_stop(self)

    monkeypatch.setattr(Worker, "stop", stop)
    return calls


def test_sigterm_during_a_claimed_job_finishes_it_then_stops(
    settings, monkeypatch, stops
):
    """The job in hand is recorded whole; the next one is left for a worker."""
    repository = Repository(settings.database_path)
    seen: list[str] = []
    built: list[_RecordingContainer] = []

    def handler(payload):
        seen.append(payload["name"])
        # Delivered to this process and handled before `raise_signal` returns,
        # so the stop request lands while this job's claim is still held.
        signal.raise_signal(signal.SIGTERM)
        return DONE

    real_container = composition.worker_container

    def worker_container(resolved: Settings):
        container = _RecordingContainer(real_container(resolved))
        worker = container.get(Worker)
        worker.runner.queue.handlers["example"] = handler
        built.append(container)
        return container

    monkeypatch.setattr(composition, "worker_container", worker_container)

    # Older than anything the schedule fires on the first tick, so claimed first.
    now = datetime.now(UTC)
    earlier = repository.jobs.enqueue(
        kind="example",
        payload={"name": "first"},
        available_at=now - timedelta(hours=2),
    )
    later = repository.jobs.enqueue(
        kind="example",
        payload={"name": "second"},
        available_at=now - timedelta(hours=1),
    )
    assert earlier is not None
    assert later is not None

    run_worker(settings)

    assert seen == ["first"]
    finished = repository.jobs.get(earlier.id)
    assert finished.status is JobStatus.SUCCEEDED
    untouched = repository.jobs.get(later.id)
    assert untouched.status is JobStatus.PENDING
    assert untouched.attempts == 0
    # Both stop signals route to the runner, and teardown ran in order.
    for received in _STOP_SIGNALS:
        assert callable(signal.getsignal(received))
    assert len(stops) == 1
    assert [container.closed for container in built] == [True]
    repository.close()


def test_a_failed_start_still_stops_the_worker_and_closes_the_container(
    settings, monkeypatch, stops, restore_signal_handlers
):
    """Startup errors propagate, after teardown, with no signal handlers left."""
    built: list[_RecordingContainer] = []
    real_container = composition.worker_container

    def worker_container(resolved: Settings):
        assert resolved is settings
        built.append(_RecordingContainer(real_container(resolved)))
        return built[-1]

    def refuse_start(_self: Worker) -> None:
        raise RuntimeError("schedules could not be registered")

    monkeypatch.setattr(composition, "worker_container", worker_container)
    monkeypatch.setattr(Settings, "from_environment", classmethod(lambda _: settings))
    monkeypatch.setattr(Worker, "start", refuse_start)

    with pytest.raises(RuntimeError, match="schedules could not be registered"):
        run_worker()

    assert len(stops) == 1
    assert [container.closed for container in built] == [True]
    for received, handler in restore_signal_handlers.items():
        assert signal.getsignal(received) is handler
