"""What a workflow records around work SQLite cannot roll back.

The journal is a real one in a dictionary: `advance` builds a `Workflow` each
time, so the model's own rule (a failed workflow explains itself) is
exercised.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from blitzecdn.capabilities.workflows.domain import (
    Workflow,
    WorkflowStatus,
    WorkflowStep,
)
from blitzecdn.capabilities.workflows.service import WorkflowCoordinator


class FakeJournal:
    """An in-memory `WorkflowJournal` that keeps the models it is given."""

    def __init__(self) -> None:
        self.workflows: dict[str, Workflow] = {}
        self.pruned_to: list[int] = []

    def create(
        self,
        workflow_id: str,
        kind: str,
        operator: str,
        resource_id: str | None = None,
    ) -> Workflow:
        now = datetime.now(UTC)
        self.workflows[workflow_id] = Workflow(
            id=workflow_id,
            kind=kind,
            resource_id=resource_id,
            status=WorkflowStatus.PENDING,
            operator=operator,
            created_at=now,
            updated_at=now,
        )
        return self.workflows[workflow_id]

    def advance(
        self,
        workflow_id: str,
        status: WorkflowStatus,
        *,
        step: WorkflowStep | None = None,
        error: str | None = None,
    ) -> Workflow:
        current = self.workflows[workflow_id]
        self.workflows[workflow_id] = current.model_copy(
            update={
                "status": status,
                "updated_at": datetime.now(UTC),
                "steps": (*current.steps, step) if step else current.steps,
                "error": error if error is not None else current.error,
            }
        )
        return self.workflows[workflow_id]

    def unfinished(self) -> list[Workflow]:
        return [
            workflow
            for workflow in self.workflows.values()
            if workflow.status in {WorkflowStatus.PENDING, WorkflowStatus.RUNNING}
        ]

    def prune_finished(self, keep: int) -> int:
        self.pruned_to.append(keep)
        return 0


class RecordingUnitOfWork:
    """Counts transactions, because when they open is part of the contract."""

    def __init__(self) -> None:
        self.opened = 0
        self.depth = 0

    @contextmanager
    def transaction(self) -> Any:
        self.opened += 1
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1


@pytest.fixture
def journal() -> FakeJournal:
    return FakeJournal()


@pytest.fixture
def uow() -> RecordingUnitOfWork:
    return RecordingUnitOfWork()


@pytest.fixture
def coordinator(journal: FakeJournal, uow: RecordingUnitOfWork):
    return WorkflowCoordinator(journal=journal, uow=uow, retention=5)


def test_a_workflow_opens_running_and_closes_succeeded(coordinator, journal):
    """The ordinary path, and the only one that ends `SUCCEEDED`."""
    with coordinator.run("deployment", "alice", "example-com") as progress:
        assert progress.error is None
        opened = next(iter(journal.workflows.values()))
        assert opened.status is WorkflowStatus.RUNNING
        assert opened.kind == "deployment"
        assert opened.operator == "alice"
        assert opened.resource_id == "example-com"

    closed = next(iter(journal.workflows.values()))
    assert closed.status is WorkflowStatus.SUCCEEDED
    assert closed.error is None


def test_each_workflow_gets_its_own_identifier(coordinator, journal):
    """Two runs are two journal entries, not one overwritten twice."""
    with coordinator.run("deployment", "alice"):
        pass
    with coordinator.run("deployment", "alice"):
        pass

    assert len(journal.workflows) == 2


def test_a_checkpoint_records_a_step_without_closing_the_workflow(coordinator, journal):
    with coordinator.run("certificate", "alice") as progress:
        progress.checkpoint("ordered", {"authority": "letsencrypt"})
        progress.checkpoint("validated")

        running = next(iter(journal.workflows.values()))
        assert running.status is WorkflowStatus.RUNNING
        assert [step.name for step in running.steps] == ["ordered", "validated"]
        assert running.steps[0].details == {"authority": "letsencrypt"}
        assert running.steps[1].details == {}


def test_work_reporting_its_own_failure_closes_failed_without_raising(
    coordinator, journal
):
    """`progress.fail` is for work that failed without an exception, such as an ACME
    order the CA rejected.
    """
    with coordinator.run("certificate", "alice") as progress:
        progress.fail("the authority refused the order")

    closed = next(iter(journal.workflows.values()))
    assert closed.status is WorkflowStatus.FAILED
    assert closed.error == "the authority refused the order"


def test_an_exception_closes_the_workflow_failed_and_still_propagates(
    coordinator, journal
):
    """Recording the failure must not swallow it: the caller still fails."""
    with (
        pytest.raises(RuntimeError, match="the fleet lock was taken"),
        coordinator.run("deployment", "alice"),
    ):
        raise RuntimeError("the fleet lock was taken")

    closed = next(iter(journal.workflows.values()))
    assert closed.status is WorkflowStatus.FAILED
    assert closed.error == "RuntimeError: the fleet lock was taken"


def test_an_interrupt_is_recorded_too(coordinator, journal):
    """`except BaseException`, deliberately.

    A worker killed with SIGINT mid-issuance is what the journal is for, and
    `KeyboardInterrupt` is not an `Exception`.
    """
    with pytest.raises(KeyboardInterrupt), coordinator.run("deployment", "alice"):
        raise KeyboardInterrupt

    closed = next(iter(journal.workflows.values()))
    assert closed.status is WorkflowStatus.FAILED
    assert closed.error == "KeyboardInterrupt: "


def test_retention_is_applied_when_a_workflow_closes(coordinator, journal):
    """The policy runs when the thing it bounds happens, not on a timer."""
    with coordinator.run("deployment", "alice"):
        pass

    assert journal.pruned_to == [5]


def test_a_failed_workflow_is_not_pruned_by_its_own_closing(coordinator, journal):
    """Pruning is in the `else` branch: a failure is what an operator reads."""
    with pytest.raises(RuntimeError), coordinator.run("deployment", "alice"):
        raise RuntimeError("boom")

    assert journal.pruned_to == []


def test_every_journal_write_happens_inside_a_transaction(coordinator, uow):
    """Open, checkpoint and close are three writes and three boundaries.

    The work in between leaves the process, so no transaction is held across it.
    """
    with coordinator.run("deployment", "alice") as progress:
        assert uow.opened == 1
        assert uow.depth == 0
        progress.checkpoint("converged")
        assert uow.opened == 2
        assert uow.depth == 0

    assert uow.opened == 3


def test_restart_recovery_turns_unfinished_work_into_something_to_read(
    coordinator, journal
):
    """What a controller does with whatever it finds still running."""
    journal.create("stranded", "certificate", "alice", "example-com")
    journal.advance("stranded", WorkflowStatus.RUNNING)
    journal.create("also-stranded", "deployment", "bob")

    recovered = coordinator.reconcile_interrupted()

    assert {workflow.id for workflow in recovered} == {"stranded", "also-stranded"}
    assert all(workflow.status is WorkflowStatus.NEEDS_REVIEW for workflow in recovered)
    assert "verify the recorded checkpoints" in recovered[0].error


def test_recovery_leaves_finished_work_alone(coordinator, journal):
    """A succeeded workflow is not unfinished, and must not be reopened."""
    with coordinator.run("deployment", "alice"):
        pass

    assert coordinator.reconcile_interrupted() == []
    assert all(
        workflow.status is WorkflowStatus.SUCCEEDED
        for workflow in journal.workflows.values()
    )


def test_recovery_reviews_in_one_transaction(coordinator, journal, uow):
    """Two entries, one boundary: recovery is a single decision.

    Per-entry transactions could leave half the journal reviewed.
    """
    journal.create("one", "deployment", "alice")
    journal.create("two", "deployment", "alice")

    coordinator.reconcile_interrupted()

    assert uow.opened == 1


def test_recovery_for_one_resource_leaves_everything_else_running(coordinator, journal):
    """What a worker may do, as opposed to what startup may.

    The API may be mid-workflow, so a worker reviews only the resource it
    recovers.
    """
    journal.create("died-in", "deployment", "alice", "dep-1")
    journal.advance("died-in", WorkflowStatus.RUNNING)
    journal.create("in-the-api", "certificate", "bob", "example-com")
    journal.advance("in-the-api", WorkflowStatus.RUNNING)

    recovered = coordinator.reconcile_interrupted(resource_id="dep-1")

    assert [workflow.id for workflow in recovered] == ["died-in"]
    assert journal.workflows["in-the-api"].status is WorkflowStatus.RUNNING
