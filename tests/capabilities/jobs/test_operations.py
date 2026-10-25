"""Edge operations on the queue: declared kinds, one run, a result to read back."""

from __future__ import annotations

import pytest

from blitzecdn.capabilities.jobs.domain import Done, JobStatus
from blitzecdn.capabilities.jobs.service import JobQueue, OperationService
from blitzecdn.composition import Repository
from blitzecdn.core.exceptions import ConflictError, NotFoundError


@pytest.fixture
def queue(settings) -> JobQueue:
    jobs = Repository(settings.database_path).jobs
    return JobQueue(jobs=jobs, handlers={}, worker_id="worker-a")


def test_an_operation_runs_once_and_keeps_its_result(queue: JobQueue):
    """No automatic retry: the operator asked for one run and reads its outcome."""
    operations = OperationService(queue=queue, kinds={"cache.stats"})
    submitted = operations.submit("cache.stats", {"operator": "ops"})
    assert submitted.max_attempts == 1

    queue.consume_with({"cache.stats": lambda _payload: Done(result={"edges": []})})
    queue.run_one()

    finished = operations.get(submitted.id)
    assert finished.status is JobStatus.SUCCEEDED
    assert finished.result == {"edges": []}


def test_an_undeclared_kind_is_neither_submitted_nor_read(queue: JobQueue):
    operations = OperationService(queue=queue, kinds={"cache.stats"})
    with pytest.raises(NotFoundError):
        operations.submit("deployment", {})

    internal = queue.enqueue_deployment("d-1")
    assert internal is not None
    with pytest.raises(NotFoundError):
        operations.get(internal.id)


def test_a_second_operation_on_a_busy_target_is_refused(queue: JobQueue):
    """Two removals of one edge would race each other's teardown."""
    operations = OperationService(queue=queue, kinds={"edges.remove"})
    operations.submit("edges.remove", {"name": "edge-a"}, dedupe_key="edge-a")
    with pytest.raises(ConflictError, match="already queued or running"):
        operations.submit("edges.remove", {"name": "edge-a"}, dedupe_key="edge-a")
