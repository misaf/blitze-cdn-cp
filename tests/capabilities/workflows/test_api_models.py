"""A workflow reads like every other queued resource: ``done`` and ``finished_at``."""

from datetime import UTC, datetime, timedelta

import pytest

from blitzecdn.capabilities.workflows.api.models import Workflow
from blitzecdn.capabilities.workflows.domain import Workflow as StoredWorkflow
from blitzecdn.capabilities.workflows.domain import WorkflowStatus
from blitzecdn.delivery.api.models import as_operation

CREATED = datetime(2026, 1, 1, tzinfo=UTC)
UPDATED = CREATED + timedelta(minutes=3)


def _stored(status: WorkflowStatus) -> StoredWorkflow:
    return StoredWorkflow(
        id="w" * 32,
        kind="restore",
        status=status,
        operator="tester",
        created_at=CREATED,
        updated_at=UPDATED,
        error="broke" if status is WorkflowStatus.FAILED else None,
    )


@pytest.mark.parametrize("status", list(WorkflowStatus))
def test_a_workflow_is_done_once_nothing_more_runs_without_an_operator(status):
    published = as_operation(_stored(status), Workflow)
    active = status in {WorkflowStatus.PENDING, WorkflowStatus.RUNNING}
    assert published.done is not active
    assert published.finished_at == (None if active else UPDATED)
    assert published.result is None
