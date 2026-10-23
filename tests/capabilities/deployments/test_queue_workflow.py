from datetime import UTC, datetime, timedelta

import pytest
from application_support import _await_terminal, _await_workflow
from control_plane_fixtures import (
    FakeRunner,
    RecordingBackgroundQueue,
    RefusingBackgroundQueue,
    ansible_run,
    host_run,
    seed_edge,
    seed_site,
)

from blitzecdn.capabilities.deployments.domain import (
    DEPLOYMENT_WORKFLOW,
    DeploymentStatus,
    TargetStatus,
)
from blitzecdn.capabilities.jobs.domain import JobStatus
from blitzecdn.capabilities.workflows.domain import WorkflowStatus
from blitzecdn.composition import ControlPlane, Repository
from blitzecdn.core.domain.runs import RunStatus
from blitzecdn.core.exceptions import ExecutionError, NotFoundError
from blitzecdn.core.plugins import ProcessKind


def test_submit_deployment_queues_and_converges_on_a_worker(settings):
    repository = Repository(settings.database_path)

    class Queue:
        def __init__(self):
            self.ids: list[str] = []

        def enqueue(self, deployment_id: str) -> None:
            self.ids.append(deployment_id)

    queue = Queue()
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner(),
        background=queue,
    )
    seed_site(control)

    queued = control.deployments.submit_deployment("alice")
    assert queued.status is DeploymentStatus.QUEUED

    assert queue.ids == [queued.id]
    control.deployments.run_queued(queue.ids.pop())
    assert repository.deployments.get_deployment(queued.id).status is (
        DeploymentStatus.SUCCEEDED
    )


def test_durable_queue_receives_only_the_deployment_id(settings):
    repository = Repository(settings.database_path)

    class Queue:
        def __init__(self):
            self.ids = []

        def enqueue(self, deployment_id):
            self.ids.append(deployment_id)

    queue = Queue()
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner(),
        background=queue,
    )
    seed_site(control)

    queued = control.deployments.submit_deployment("alice")

    assert queue.ids == [queued.id]
    assert (
        repository.deployments.get_deployment(queued.id).status
        is DeploymentStatus.QUEUED
    )
    # Startup republishes queued identifiers. Not for a crash between commit
    # and publish — those are one commit now — but for a QUEUED deployment
    # whose job ran out of attempts finding the lock busy.
    assert control.deployments.initialize() == 0
    assert queue.ids == [queued.id, queued.id]


def test_durable_queue_delivery_is_idempotent(settings):
    repository = Repository(settings.database_path)

    class Queue:
        def enqueue(self, deployment_id):
            pass

    runner = FakeRunner()
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=runner,
        background=Queue(),
    )
    seed_edge(control, name="edge-a")
    seed_site(control)
    queued = control.deployments.submit_deployment("alice")

    first = control.deployments.run_queued(queued.id)
    duplicate = control.deployments.run_queued(queued.id)

    assert first.status is DeploymentStatus.SUCCEEDED
    assert duplicate.status is DeploymentStatus.SUCCEEDED
    # One rollout, not two: the second delivery finds the deployment is no
    # longer QUEUED and converges nothing. The three runs are the one edge's
    # validate, stage and activate phases.
    assert runner.check_modes == [True, False, False]
    assert runner.tag_selections == [(), ("stage",), ()]


def test_a_queued_deployment_leaves_a_workflow_record(settings):
    """The queued path is the one that most needs a durable trace.

    It answers before the convergence happens, so the run outlives the call
    that started it and a restart in between is exactly what the workflow
    journal exists to make legible. The synchronous path recorded one and this
    one did not, which had it backwards.
    """
    repository = Repository(settings.database_path)
    queue = RecordingBackgroundQueue()
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner(),
        background=queue,
    )
    seed_site(control)

    queued = control.deployments.submit_deployment("alice")
    control.deployments.run_queued(queue.ids.pop())
    assert _await_terminal(repository, queued.id) is DeploymentStatus.SUCCEEDED

    assert _await_workflow(repository, queued.id) is WorkflowStatus.SUCCEEDED
    workflows = repository.workflows.list_workflows(10)
    assert [workflow.kind for workflow in workflows] == [DEPLOYMENT_WORKFLOW]
    assert workflows[0].resource_id == queued.id
    assert [step.name for step in workflows[0].steps] == ["converged"]


def test_a_failed_queued_deployment_fails_its_workflow(settings):
    """A workflow that succeeded while its deployment failed would mislead."""
    repository = Repository(settings.database_path)
    queue = RecordingBackgroundQueue()
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner(
            [
                ansible_run(
                    host_run("edge-a", failed=1, failure="nginx -t refused it"),
                    status=RunStatus.FAILED,
                    return_code=2,
                )
            ]
        ),
        background=queue,
    )
    seed_edge(control, name="edge-a")
    seed_site(control)

    queued = control.deployments.submit_deployment("alice")
    control.deployments.run_queued(queue.ids.pop())
    assert _await_terminal(repository, queued.id) is DeploymentStatus.FAILED

    assert _await_workflow(repository, queued.id) is WorkflowStatus.FAILED
    assert repository.workflows.list_workflows(10)[0].error


def test_submit_rollback_reports_conflicts_synchronously(settings):
    """Nothing to roll back to must surface as an error, not a queued record."""
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings, repository=repository, runner=FakeRunner()
    )
    with pytest.raises(NotFoundError):
        control.deployments.submit_rollback("alice")


def test_submit_releases_the_lock_after_queue_publication(settings):
    repository = Repository(settings.database_path)
    queue = RecordingBackgroundQueue()
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner(),
        background=queue,
    )
    seed_site(control)

    queued = control.deployments.submit_deployment("alice")
    control.deployments.run_queued(queue.ids.pop())
    assert _await_terminal(repository, queued.id) is DeploymentStatus.SUCCEEDED
    # A second submission proves the publisher handed the lock back.
    control._runner.results = [ansible_run(host_run("edge-a"))]
    again = control.deployments.submit_deployment("alice")
    control.deployments.run_queued(queue.ids.pop())
    assert _await_terminal(repository, again.id) is DeploymentStatus.SUCCEEDED


def test_a_queued_deployment_converges_when_its_identifier_is_delivered(settings):
    repository = Repository(settings.database_path)
    queue = RecordingBackgroundQueue()
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner(),
        background=queue,
    )
    seed_site(control)

    queued = control.deployments.submit_deployment("alice")
    control.deployments.run_queued(queue.ids.pop())

    assert repository.deployments.get_deployment(queued.id).status is (
        DeploymentStatus.SUCCEEDED
    )


def test_a_worker_that_cannot_start_does_not_strand_the_lock(settings):
    """A transient failure must not become a permanent outage.

    The lock is released by whoever owns the deployment, and until the worker is
    running that is still `submit`. Work that never starts used to leave it held
    by a worker that did not exist, and every later deploy, rollback, upload and
    issuance failed with DeploymentBusyError until someone restarted the
    process.

    Driven through the durable queue port: a refused enqueue is part of that
    adapter's contract. Since the deployment and its job commit together, the
    refusal rolls the deployment back with it — there is no QUEUED row nothing
    will deliver, and no FAILED row to explain one.
    """
    repository = Repository(settings.database_path)
    refusing = RefusingBackgroundQueue()
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner(),
        background=refusing,
    )
    seed_site(control)

    with pytest.raises(RuntimeError):
        control.deployments.submit_deployment("alice")

    # Nothing was recorded: the deployment row went with the job it could not
    # be given, rather than surviving as QUEUED forever or FAILED after the fact.
    assert repository.deployments.list_deployments(10) == []

    # And the lock came back, so the control plane still works.
    refusing.refuse = False
    control._runner.results = [ansible_run(host_run("edge-a"))]
    again = control.deployments.submit_deployment("alice")
    control.deployments.run_queued(refusing.ids.pop())
    assert _await_terminal(repository, again.id) is DeploymentStatus.SUCCEEDED


def test_runner_errors_are_recorded_and_reraised(settings):
    class ExplodingRunner(FakeRunner):
        def run(self, *, check, host_limit=None, tags=()):
            raise ExecutionError("unable to execute Ansible")

    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings, repository=repository, runner=ExplodingRunner()
    )
    seed_edge(control, name="edge-a")
    seed_site(control)

    with pytest.raises(ExecutionError):
        control.deployments.deploy("alice")

    recorded = repository.deployments.list_deployments(1)[0]
    assert recorded.status is DeploymentStatus.FAILED
    # The edge's own row says which phase it died in, so an operator does not
    # have to read a fleet-wide result to learn that nothing was converged.
    (target,) = repository.deployment_targets.targets(recorded.id)
    assert target.status is TargetStatus.FAILED
    assert "validate failed" in (target.last_error or "")
    # The runner raised before Ansible reported anything, so the deployment
    # carries a synthesised result rather than none: every reader looks in the
    # same place for why a deployment ended.
    assert recorded.result is not None
    assert recorded.result.status is RunStatus.UNSTARTED
    assert "unable to execute Ansible" in (recorded.result.error or "")
    assert any(
        event.action == "deployment.failed"
        for event in repository.audit_log.list_audit_events(10)
    )


def test_worker_survives_a_runner_error_and_releases_the_lock(settings):
    """An exception in a worker must not strand the deployment lock."""
    repository = Repository(settings.database_path)
    calls: list[int] = []

    class ExplodingOnceRunner(FakeRunner):
        def run(self, *, check, host_limit=None, tags=()):
            calls.append(1)
            if len(calls) == 1:
                raise ExecutionError("boom")
            return ansible_run(host_run("edge-a"))

    class Queue:
        def __init__(self):
            self.ids: list[str] = []

        def enqueue(self, deployment_id: str) -> None:
            self.ids.append(deployment_id)

    queue = Queue()
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=ExplodingOnceRunner(),
        background=queue,
    )
    seed_edge(control, name="edge-a")
    seed_site(control)

    first = control.deployments.submit_deployment("alice")
    with pytest.raises(ExecutionError, match="boom"):
        control.deployments.run_queued(queue.ids.pop(0))
    assert repository.deployments.get_deployment(first.id).status is (
        DeploymentStatus.FAILED
    )

    second = control.deployments.submit_deployment("alice")
    control.deployments.run_queued(queue.ids.pop(0))
    assert repository.deployments.get_deployment(second.id).status is (
        DeploymentStatus.SUCCEEDED
    )


def test_a_deployment_its_worker_died_in_is_abandoned_on_redelivery(settings):
    """A SIGKILL runs no handler, so a dead worker leaves two things behind.

    The deployment RUNNING, and its job leased until the lease runs out. The
    API's startup recovery is the only thing that used to abandon such a
    deployment, so a worker that crashed and came back under a running API left
    it RUNNING indefinitely — and the redelivered job found it not QUEUED, did
    nothing, and was recorded as succeeded.

    Now the redelivery, holding the lock nobody else can be holding, abandons
    the deployment, closes its journal entry and only its, and fails the job
    permanently with the reason.
    """
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner(),
        process=ProcessKind.WORKER,
    )
    seed_site(control)
    queued = control.deployments.submit_deployment("alice")

    # Worker A claims the job and gets as far as converge's first writes —
    # the journal entry, then RUNNING — and is killed.
    claim = control.job_queue.claim(lease_seconds=1)
    assert claim is not None
    with repository.transaction():
        repository.workflows.create("died-in", DEPLOYMENT_WORKFLOW, "alice", queued.id)
        repository.workflows.advance("died-in", WorkflowStatus.RUNNING)
        repository.workflows.create("in-the-api", "certificate", "bob", "example-com")
        repository.workflows.advance("in-the-api", WorkflowStatus.RUNNING)
        repository.deployments.transition(
            queued.id, DeploymentStatus.QUEUED, DeploymentStatus.RUNNING
        )
    later = datetime.now(UTC) + timedelta(seconds=5)
    control.job_queue.clock = lambda: later

    # Worker B, the restarted container, takes the expired job.
    assert control.job_runner.tick() is True

    deployment = repository.deployments.get_deployment(queued.id)
    assert deployment.status is DeploymentStatus.ABANDONED
    assert "worker converging this deployment stopped" in (deployment.detail or "")
    job = control.job_queue.get(claim.job.id)
    assert job.status is JobStatus.FAILED
    assert job.attempts == 2  # failed at once, not retried into a no-op
    assert f"deployment {queued.id} was abandoned" in (job.last_error or "")
    assert repository.workflows.get("died-in").status is WorkflowStatus.NEEDS_REVIEW
    assert repository.workflows.get("in-the-api").status is WorkflowStatus.RUNNING
    assert any(
        event.action == "deployment.abandoned" and event.resource_id == queued.id
        for event in repository.audit_log.list_audit_events(10)
    )


def test_a_job_for_a_deployment_startup_recovery_abandoned_fails_too(settings):
    """The API restarted first, abandoned it, and then the job came round.

    Not a duplicate delivery of finished work: the work was never done, and
    the job says so rather than recording a success.
    """
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner(),
        process=ProcessKind.WORKER,
    )
    seed_site(control)
    queued = control.deployments.submit_deployment("alice")
    claim = control.job_queue.claim(lease_seconds=1)
    assert claim is not None
    repository.deployments.transition(
        queued.id, DeploymentStatus.QUEUED, DeploymentStatus.RUNNING
    )
    assert control.deployments.initialize() == 1
    later = datetime.now(UTC) + timedelta(seconds=5)
    control.job_queue.clock = lambda: later

    control.job_runner.tick()

    assert control.job_queue.get(claim.job.id).status is JobStatus.FAILED


def test_a_deployment_and_its_job_commit_together_or_not_at_all(settings, monkeypatch):
    """The real job queue, failing after its own row was written.

    The Redis-era shape committed the deployment and then published its job,
    so a crash between the two stranded a QUEUED row. With the job a row in the
    same database the pair is one commit, and this holds it at the worst
    moment: the job is in the session when the failure comes.
    """
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner(),
        process=ProcessKind.WORKER,
    )
    seed_site(control)
    queue_job = control.job_queue.enqueue_deployment

    def enqueue_then_fail(deployment_id):
        queue_job(deployment_id)
        raise RuntimeError("the process died here")

    monkeypatch.setattr(control.job_queue, "enqueue_deployment", enqueue_then_fail)
    with pytest.raises(RuntimeError):
        control.deployments.submit_deployment("alice")

    assert repository.deployments.list_deployments(10) == []
    assert control.job_queue.list_jobs() == []

    monkeypatch.undo()
    queued = control.deployments.submit_deployment("alice")
    (job,) = control.job_queue.list_jobs()
    assert job.payload == {"deployment_id": queued.id}
