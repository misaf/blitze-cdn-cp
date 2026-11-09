"""Edge operations on the queue: declared kinds, one run, a result to read back."""

from __future__ import annotations

import pytest
from support.stores import Stores

from blitzecdn.capabilities.cache.service.operations import CacheConnectionsJob
from blitzecdn.capabilities.edges.service.operations import EdgeRemovalJob
from blitzecdn.capabilities.jobs.domain import DeploymentJob, Done, JobStatus
from blitzecdn.capabilities.jobs.ports import JobHandler
from blitzecdn.capabilities.jobs.service import JobQueue, OperationService
from blitzecdn.core.exceptions import ConflictError, NotFoundError
from blitzecdn.core.ports.clock import system_clock


@pytest.fixture
def queue(settings) -> JobQueue:
    jobs = Stores(settings.database_path).jobs
    return JobQueue(jobs=jobs, handlers=(), worker_id="worker-a", clock=system_clock)


def test_an_operation_runs_once_and_keeps_its_result(queue: JobQueue):
    """No automatic retry: the operator asked for one run and reads its outcome."""
    operations = OperationService(queue=queue, kinds={"cache.connections"})
    submitted = operations.submit(CacheConnectionsJob(operator="ops"))
    assert submitted.max_attempts == 1

    queue.consume_with(
        (JobHandler(CacheConnectionsJob, lambda _job: Done(result={"edges": []})),)
    )
    queue.run_one()

    finished = operations.get(submitted.id)
    assert finished.status is JobStatus.SUCCEEDED
    assert finished.result == {"edges": []}


def test_an_undeclared_kind_is_neither_submitted_nor_read(queue: JobQueue):
    operations = OperationService(queue=queue, kinds={"cache.connections"})
    with pytest.raises(NotFoundError):
        operations.submit(DeploymentJob(deployment_id="d-1"))

    internal = queue.enqueue_deployment("d-1")
    assert internal is not None
    with pytest.raises(NotFoundError):
        operations.get(internal.id)


def test_a_second_operation_on_a_busy_target_is_refused(queue: JobQueue):
    """Two removals of one edge would race each other's teardown."""
    operations = OperationService(queue=queue, kinds={"edges.remove"})
    removal = EdgeRemovalJob(operator="ops", name="edge-a", decommission=False)
    operations.submit(removal, dedupe_key="edge-a")
    with pytest.raises(ConflictError, match="already queued or running"):
        operations.submit(removal, dedupe_key="edge-a")
