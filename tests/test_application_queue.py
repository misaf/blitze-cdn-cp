# ruff: noqa: F403,F405
from application_support import *


def test_submit_deployment_queues_and_converges_on_a_worker(settings, site_payload):
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
        runner=FakeRunner(),  # type: ignore[arg-type]
        background=queue,
    )
    repository.sites.create_site(CdnSite.model_validate(site_payload))

    queued = control.deployments.submit_deployment("alice")
    assert queued.status is DeploymentStatus.QUEUED

    assert queue.ids == [queued.id]
    control.deployments.run_queued(queue.ids.pop())
    assert repository.deployments.get_deployment(queued.id).status is (
        DeploymentStatus.SUCCEEDED
    )


def test_durable_queue_receives_only_the_deployment_id(settings, site_payload):
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
        runner=FakeRunner(),  # type: ignore[arg-type]
        background=queue,
    )
    repository.sites.create_site(CdnSite.model_validate(site_payload))

    queued = control.deployments.submit_deployment("alice")

    assert queue.ids == [queued.id]
    assert (
        repository.deployments.get_deployment(queued.id).status
        is DeploymentStatus.QUEUED
    )
    # Redis publish and the SQLite commit cannot be atomic. Startup republishes
    # queued identifiers so a crash between them cannot strand the record.
    assert control.deployments.initialize() == 0
    assert queue.ids == [queued.id, queued.id]


def test_durable_queue_delivery_is_idempotent(settings, site_payload):
    repository = Repository(settings.database_path)

    class Queue:
        def enqueue(self, deployment_id):
            pass

    runner = FakeRunner()
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=runner,  # type: ignore[arg-type]
        background=Queue(),
    )
    repository.sites.create_site(CdnSite.model_validate(site_payload))
    queued = control.deployments.submit_deployment("alice")

    first = control.deployments.run_queued(queued.id)
    duplicate = control.deployments.run_queued(queued.id)

    assert first.status is DeploymentStatus.SUCCEEDED
    assert duplicate.status is DeploymentStatus.SUCCEEDED
    assert runner.check_modes == [False]


def test_a_queued_deployment_leaves_a_workflow_record(settings, site_payload):
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
        runner=FakeRunner(),  # type: ignore[arg-type]
        background=queue,
    )
    repository.sites.create_site(CdnSite.model_validate(site_payload))

    queued = control.deployments.submit_deployment("alice")
    control.deployments.run_queued(queue.ids.pop())
    assert _await_terminal(repository, queued.id) is DeploymentStatus.SUCCEEDED

    assert _await_workflow(repository, queued.id) is WorkflowStatus.SUCCEEDED
    workflows = repository.workflows.list_workflows(10)
    assert [workflow.kind for workflow in workflows] == [WorkflowKind.DEPLOYMENT]
    assert workflows[0].resource_id == queued.id
    assert [step.name for step in workflows[0].steps] == ["converged"]


def test_a_failed_queued_deployment_fails_its_workflow(settings, site_payload):
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
    )  # type: ignore[arg-type]
    repository.sites.create_site(CdnSite.model_validate(site_payload))

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
    )  # type: ignore[arg-type]
    with pytest.raises(NotFoundError):
        control.deployments.submit_rollback("alice")


def test_submit_releases_the_lock_after_queue_publication(settings, site_payload):
    repository = Repository(settings.database_path)
    queue = RecordingBackgroundQueue()
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner(),  # type: ignore[arg-type]
        background=queue,
    )
    repository.sites.create_site(CdnSite.model_validate(site_payload))

    queued = control.deployments.submit_deployment("alice")
    control.deployments.run_queued(queue.ids.pop())
    assert _await_terminal(repository, queued.id) is DeploymentStatus.SUCCEEDED
    # A second submission proves the publisher handed the lock back.
    control._runner.results = [ansible_run(host_run("edge-a"))]
    again = control.deployments.submit_deployment("alice")
    control.deployments.run_queued(queue.ids.pop())
    assert _await_terminal(repository, again.id) is DeploymentStatus.SUCCEEDED


def test_a_queued_deployment_converges_when_its_identifier_is_delivered(
    settings, site_payload
):
    repository = Repository(settings.database_path)
    queue = RecordingBackgroundQueue()
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner(),  # type: ignore[arg-type]
        background=queue,
    )
    repository.sites.create_site(CdnSite.model_validate(site_payload))

    queued = control.deployments.submit_deployment("alice")
    control.deployments.run_queued(queue.ids.pop())

    assert repository.deployments.get_deployment(queued.id).status is (
        DeploymentStatus.SUCCEEDED
    )


def test_a_worker_that_cannot_start_does_not_strand_the_lock(settings, site_payload):
    """A transient failure must not become a permanent outage.

    The lock is released by whoever owns the deployment, and until the worker is
    running that is still `submit`. Work that never starts used to leave it held
    by a worker that did not exist, and every later deploy, rollback, upload and
    issuance failed with DeploymentBusyError until someone restarted the
    process.

    Driven through the durable queue port rather than by monkeypatching
    Dramatiq: publication failure is part of that adapter's contract.
    """
    repository = Repository(settings.database_path)
    refusing = RefusingBackgroundQueue()
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner(),  # type: ignore[arg-type]
        background=refusing,
    )
    repository.sites.create_site(CdnSite.model_validate(site_payload))

    with pytest.raises(RuntimeError):
        control.deployments.submit_deployment("alice")

    # The deployment is recorded as failed rather than left QUEUED forever.
    recorded = repository.deployments.list_deployments(1)[0]
    assert recorded.status is DeploymentStatus.FAILED
    assert recorded.result is not None
    assert recorded.result.status is RunStatus.UNSTARTED

    # And the lock came back, so the control plane still works.
    refusing.refuse = False
    control._runner.results = [ansible_run(host_run("edge-a"))]
    again = control.deployments.submit_deployment("alice")
    control.deployments.run_queued(refusing.ids.pop())
    assert _await_terminal(repository, again.id) is DeploymentStatus.SUCCEEDED


def test_runner_errors_are_recorded_and_reraised(settings, site_payload):
    class ExplodingRunner(FakeRunner):
        def run(self, *, check, host_limit=None):
            raise ExecutionError("unable to execute Ansible")

    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings, repository=repository, runner=ExplodingRunner()
    )  # type: ignore[arg-type]
    repository.sites.create_site(CdnSite.model_validate(site_payload))

    with pytest.raises(ExecutionError):
        control.deployments.deploy("alice")

    recorded = repository.deployments.list_deployments(1)[0]
    assert recorded.status is DeploymentStatus.FAILED
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


def test_worker_survives_a_runner_error_and_releases_the_lock(settings, site_payload):
    """An exception in a worker must not strand the deployment lock."""
    repository = Repository(settings.database_path)
    calls: list[int] = []

    class ExplodingOnceRunner(FakeRunner):
        def run(self, *, check, host_limit=None):
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
        runner=ExplodingOnceRunner(),  # type: ignore[arg-type]
        background=queue,
    )
    repository.sites.create_site(CdnSite.model_validate(site_payload))

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
