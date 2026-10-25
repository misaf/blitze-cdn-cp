import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from application_support import _await_terminal, _await_workflow
from control_plane_fixtures import (
    FakeRunner,
    FakeServingProbe,
    RecordingBackgroundQueue,
    RefusingBackgroundQueue,
    ansible_run,
    build_application,
    host_run,
    seed_edge,
    seed_record,
    seed_site,
)

from blitzecdn.capabilities.deployments.domain import (
    DEPLOYMENT_WORKFLOW,
    PHASE_ORDER,
    DeploymentStatus,
    TargetPhase,
    TargetStatus,
)
from blitzecdn.capabilities.jobs.domain import JobStatus
from blitzecdn.capabilities.workflows.domain import WorkflowStatus
from blitzecdn.composition import Repository, build_worker
from blitzecdn.core.ansible.lock import DeploymentLock
from blitzecdn.core.domain.runs import RunStatus
from blitzecdn.core.exceptions import ExecutionError, NotFoundError


def test_submit_deployment_queues_and_converges_on_a_worker(settings):
    repository = Repository(settings.database_path)

    class Queue:
        def __init__(self):
            self.ids: list[str] = []

        def enqueue(self, deployment_id: str) -> None:
            self.ids.append(deployment_id)

    queue = Queue()
    control = build_application(
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
    control = build_application(
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
    control = build_application(
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
    # longer QUEUED and converges nothing. The four runs are preview, stage,
    # activation and the externally verified commit.
    assert runner.check_modes == [True, False, False, False]
    assert runner.tag_selections == [(), ("stage",), ("activate",), ("commit",)]


def test_a_queued_deployment_leaves_a_workflow_record(settings):
    """The queued path is the one that most needs a durable trace.

    It answers before the convergence happens, so the run outlives the call
    that started it and a restart in between is exactly what the workflow
    journal exists to make legible. The synchronous path recorded one and this
    one did not, which had it backwards.
    """
    repository = Repository(settings.database_path)
    queue = RecordingBackgroundQueue()
    control = build_application(
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
    control = build_application(
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


@pytest.mark.parametrize("rollback", [False, True])
@pytest.mark.parametrize("failure", ["ansible", "timeout", "verification", "runner"])
def test_terminal_deployment_failure_fails_its_real_job_without_retry(
    settings, monkeypatch, rollback, failure
):
    repository = Repository(settings.database_path)
    runner = FakeRunner()
    probe = FakeServingProbe()
    control = build_application(
        settings=settings,
        repository=repository,
        runner=runner,
        serving_probe=probe,
    )
    build_worker(control)
    seed_edge(control, name="edge-a")
    seed_site(control)
    if rollback:
        target = control.deployments.deploy("alice")
        queued = control.deployments.submit_rollback("alice", target.id)
    else:
        queued = control.deployments.submit_deployment("alice")
    if failure == "runner":

        def cannot_execute(**kwargs):
            raise ExecutionError("runner unavailable")

        monkeypatch.setattr(runner, "run", cannot_execute)
    elif failure == "verification":
        probe.served = False
    else:
        runner.results = [
            ansible_run(
                host_run("edge-a", failed=1, failure="configuration refused"),
                status=RunStatus.TIMED_OUT
                if failure == "timeout"
                else RunStatus.FAILED,
                return_code=124 if failure == "timeout" else 2,
            )
        ]

    assert control.job_runner.tick()

    result = control.deployments.get_deployment(queued.id)
    expected = (
        DeploymentStatus.TIMED_OUT if failure == "timeout" else DeploymentStatus.FAILED
    )
    assert result.status is expected
    (job,) = control.job_queue.list_jobs()
    assert job.status is JobStatus.FAILED
    assert job.attempts == 1
    assert job.dedupe_key is None
    assert result.id in job.last_error
    assert expected.value in job.last_error
    assert result.detail in job.last_error
    assert not control.job_runner.tick()


@pytest.mark.parametrize(
    "terminal",
    [
        DeploymentStatus.SUCCEEDED,
        DeploymentStatus.FAILED,
        DeploymentStatus.TIMED_OUT,
        DeploymentStatus.ABANDONED,
    ],
)
def test_redelivered_job_reports_the_saved_terminal_deployment(settings, terminal):
    repository = Repository(settings.database_path)
    runner = FakeRunner()
    control = build_application(
        settings=settings,
        repository=repository,
        runner=runner,
    )
    build_worker(control)
    seed_edge(control, name="edge-a")
    seed_site(control)
    queued = control.deployments.submit_deployment("alice")
    claim = control.job_queue.claim(lease_seconds=1)
    assert claim is not None
    repository.deployments.transition(
        queued.id, DeploymentStatus.QUEUED, DeploymentStatus.RUNNING
    )
    repository.deployments.transition(
        queued.id,
        DeploymentStatus.RUNNING,
        terminal,
        error=None
        if terminal is DeploymentStatus.SUCCEEDED
        else "saved failure detail",
    )
    later = datetime.now(UTC) + timedelta(seconds=5)
    control.job_queue.clock = lambda: later

    assert control.job_runner.tick()

    job = control.job_queue.get(claim.job.id)
    expected = (
        JobStatus.SUCCEEDED
        if terminal is DeploymentStatus.SUCCEEDED
        else JobStatus.FAILED
    )
    assert job.status is expected
    assert job.attempts == 2
    assert runner.host_limits == []
    assert control.deployments.get_deployment(queued.id).status is terminal
    if terminal is not DeploymentStatus.SUCCEEDED:
        assert "saved failure detail" in job.last_error
    assert not control.job_runner.tick()


@pytest.mark.parametrize("failure", [ExecutionError, RuntimeError])
def test_delivery_errors_before_deployment_execution_remain_retryable(
    settings, monkeypatch, failure
):
    repository = Repository(settings.database_path)
    control = build_application(
        settings=settings,
        repository=repository,
        runner=FakeRunner(),
    )
    build_worker(control)
    seed_edge(control, name="edge-a")
    seed_site(control)
    queued = control.deployments.submit_deployment("alice")
    run = control.deployments.run_queued
    calls = []

    def fail_once(deployment_id):
        calls.append(deployment_id)
        if len(calls) == 1:
            raise failure("delivery unavailable")
        return run(deployment_id)

    monkeypatch.setattr(control.deployments, "run_queued", fail_once)
    assert control.job_runner.tick()
    (waiting,) = control.job_queue.list_jobs()
    assert waiting.status is JobStatus.PENDING
    assert waiting.attempts == 1
    assert (
        control.deployments.get_deployment(queued.id).status is DeploymentStatus.QUEUED
    )
    control.job_queue.clock = lambda: waiting.available_at

    assert control.job_runner.tick()
    assert control.job_queue.get(waiting.id).status is JobStatus.SUCCEEDED
    assert control.job_queue.get(waiting.id).attempts == 2
    assert (
        control.deployments.get_deployment(queued.id).status
        is DeploymentStatus.SUCCEEDED
    )


def test_submit_rollback_reports_conflicts_synchronously(settings):
    """Nothing to roll back to must surface as an error, not a queued record."""
    repository = Repository(settings.database_path)
    control = build_application(
        settings=settings, repository=repository, runner=FakeRunner()
    )
    with pytest.raises(NotFoundError):
        control.deployments.submit_rollback("alice")


def test_submit_releases_the_lock_after_queue_publication(settings):
    repository = Repository(settings.database_path)
    queue = RecordingBackgroundQueue()
    control = build_application(
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
    control.infrastructure.runner.results = [ansible_run(host_run("edge-a"))]
    again = control.deployments.submit_deployment("alice")
    control.deployments.run_queued(queue.ids.pop())
    assert _await_terminal(repository, again.id) is DeploymentStatus.SUCCEEDED


@pytest.mark.parametrize("rollback", [False, True])
def test_a_busy_lock_defers_the_same_job_until_execution_can_start(settings, rollback):
    class LockedRunner(FakeRunner):
        def lock(self):
            return DeploymentLock(settings.deployment_lock_path)

    repository = Repository(settings.database_path)
    control = build_application(
        settings=settings,
        repository=repository,
        runner=LockedRunner(),
    )
    build_worker(control)
    seed_edge(control, name="edge-a")
    seed_site(control)
    if rollback:
        target = control.deployments.deploy("alice")
        queued = control.deployments.submit_rollback("alice", target.id)
    else:
        queued = control.deployments.submit_deployment("alice")
    now = datetime.now(UTC)
    control.job_queue.clock = lambda: now
    (original,) = control.job_queue.list_jobs()

    with DeploymentLock(settings.deployment_lock_path):
        for _ in range(original.max_attempts + 2):
            assert control.job_runner.tick()
            (waiting,) = control.job_queue.list_jobs()
            assert waiting.id == original.id
            assert waiting.status is JobStatus.PENDING
            assert waiting.attempts == 0
            assert waiting.dedupe_key == original.dedupe_key
            assert "another deployment" in waiting.last_error
            assert waiting.available_at > now
            assert not control.job_runner.tick()
            assert (
                control.deployments.get_deployment(queued.id).status
                is DeploymentStatus.QUEUED
            )
            now = waiting.available_at

    assert control.job_runner.tick()
    (finished,) = control.job_queue.list_jobs()
    assert finished.id == original.id
    assert finished.status is JobStatus.SUCCEEDED
    assert finished.attempts == 1
    assert (
        control.deployments.get_deployment(queued.id).status
        is DeploymentStatus.SUCCEEDED
    )


def test_a_queued_deployment_converges_when_its_identifier_is_delivered(settings):
    repository = Repository(settings.database_path)
    queue = RecordingBackgroundQueue()
    control = build_application(
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
    control = build_application(
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
    control.infrastructure.runner.results = [ansible_run(host_run("edge-a"))]
    again = control.deployments.submit_deployment("alice")
    control.deployments.run_queued(refusing.ids.pop())
    assert _await_terminal(repository, again.id) is DeploymentStatus.SUCCEEDED


def test_runner_errors_are_recorded_and_reraised(settings):
    class ExplodingRunner(FakeRunner):
        def run(self, *, check, host_limit=None, tags=()):
            raise ExecutionError("unable to execute Ansible")

    repository = Repository(settings.database_path)
    control = build_application(
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
    control = build_application(
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


def test_a_deployment_its_worker_died_in_resumes_on_redelivery(settings):
    """An expired job resumes the deployment and reconciles only its journal."""
    repository = Repository(settings.database_path)
    control = build_application(
        settings=settings,
        repository=repository,
        runner=FakeRunner(),
    )
    build_worker(control)
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
    assert deployment.status is DeploymentStatus.SUCCEEDED
    job = control.job_queue.get(claim.job.id)
    assert job.status is JobStatus.SUCCEEDED
    assert job.attempts == 2
    assert job.last_error is None
    assert repository.workflows.get("died-in").status is WorkflowStatus.NEEDS_REVIEW
    assert repository.workflows.get("in-the-api").status is WorkflowStatus.RUNNING
    assert any(
        event.action == "deployment.resumed" and event.resource_id == queued.id
        for event in repository.audit_log.list_audit_events(10)
    )


def test_startup_republishes_an_interrupted_deployment_for_resumption(settings):
    """Startup and worker redelivery use the same durable recovery path."""
    repository = Repository(settings.database_path)
    control = build_application(
        settings=settings,
        repository=repository,
        runner=FakeRunner(),
    )
    build_worker(control)
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

    assert control.job_queue.get(claim.job.id).status is JobStatus.SUCCEEDED


@pytest.mark.parametrize("rollback", [False, True])
def test_a_deployment_and_its_job_commit_together_or_not_at_all(
    settings, monkeypatch, rollback
):
    """The real job queue, failing after its own row was written.

    The Redis-era shape committed the deployment and then published its job,
    so a crash between the two stranded a QUEUED row. With the job a row in the
    same database the pair is one commit, and this holds it at the worst
    moment: the job is in the session when the failure comes.
    """
    repository = Repository(settings.database_path)
    control = build_application(
        settings=settings,
        repository=repository,
        runner=FakeRunner(),
    )
    build_worker(control)
    seed_edge(control, name="edge-a")
    seed_site(control)
    if rollback:
        target = control.deployments.deploy("alice")

    def submit():
        if rollback:
            return control.deployments.submit_rollback("alice", target.id)
        return control.deployments.submit_deployment("alice")

    before = repository.deployments.list_deployments(10)
    events_before = repository.audit_log.list_audit_events()
    failed_ids = []
    queue_job = control.job_queue.enqueue_deployment

    def enqueue_then_fail(deployment_id):
        assert repository.database.current_session() is not None
        assert [
            target.edge
            for target in repository.deployment_targets.targets(deployment_id)
        ] == ["edge-a"]
        failed_ids.append(deployment_id)
        queue_job(deployment_id)
        raise RuntimeError("the process died here")

    monkeypatch.setattr(control.job_queue, "enqueue_deployment", enqueue_then_fail)
    with pytest.raises(RuntimeError):
        submit()

    assert repository.deployments.list_deployments(10) == before
    assert control.job_queue.list_jobs() == []
    assert repository.deployment_targets.targets(failed_ids[0]) == []
    assert repository.audit_log.list_audit_events() == events_before

    monkeypatch.undo()
    queued = submit()
    (job,) = control.job_queue.list_jobs()
    assert job.payload == {"deployment_id": queued.id}


@pytest.mark.parametrize("check", [False, True])
def test_submission_allows_an_independent_writer_during_rendering(
    settings, monkeypatch, check
):
    repository = Repository(settings.database_path)
    control = build_application(
        settings=settings, repository=repository, runner=FakeRunner()
    )
    seed_site(control)
    probe = "blitzecdn_test_render_probe"
    repository.ansible_settings.set_setting(probe, "before")
    renderer = control.releases.renderer
    rendered = []

    class ConcurrentWriterRenderer:
        def render(self, document):
            with closing(sqlite3.connect(settings.database_path, timeout=0)) as other:
                # Retention is committed before rendering, and another
                # connection can write without waiting for rendering to end.
                other.execute(
                    "UPDATE ansible_settings SET value = ? WHERE name = ?",
                    ('"after"', probe),
                )
                assert other.execute("SELECT count(*) FROM releases").fetchone() == (1,)
                other.commit()
            rendered.append(document)
            return renderer.render(document)

    monkeypatch.setattr(control.releases, "renderer", ConcurrentWriterRenderer())

    queued = control.deployments.submit_deployment("alice", check=check)

    assert len(rendered) == 1
    assert control.releases.inputs_for(queued.release_id).settings[probe] == "before"
    assert repository.ansible_settings.list_settings()[probe] == "after"
    assert control.releases.configuration(queued.release_id)
    (job,) = control.job_queue.list_jobs()
    assert job.payload == {"deployment_id": queued.id}


def test_rendering_failure_leaves_no_deployment_or_job(settings, monkeypatch):
    repository = Repository(settings.database_path)
    control = build_application(
        settings=settings, repository=repository, runner=FakeRunner()
    )
    seed_site(control)
    renderer = control.releases.renderer

    class FailingRenderer:
        def render(self, document):
            raise RuntimeError("rendering failed")

    monkeypatch.setattr(control.releases, "renderer", FailingRenderer())

    with pytest.raises(RuntimeError, match="rendering failed"):
        control.deployments.submit_deployment("alice")

    assert repository.deployments.list_deployments() == []
    assert control.job_queue.list_jobs() == []
    # A retained release without a rendering is repairable on the next try.
    monkeypatch.setattr(control.releases, "renderer", renderer)
    queued = control.deployments.submit_deployment("alice")
    assert len(control.releases.list_releases()) == 1
    assert control.releases.configuration(queued.release_id)


def test_admission_records_targets_before_worker_delivery(settings):
    repository = Repository(settings.database_path)
    runner = FakeRunner()
    control = build_application(settings=settings, repository=repository, runner=runner)
    seed_edge(control, name="edge-a")
    seed_site(control)

    queued = control.deployments.submit_deployment("alice")
    assert [target.edge for target in control.deployments.targets(queued.id)] == [
        "edge-a"
    ]
    seed_edge(control, name="edge-b", host="192.0.2.11")
    result = control.deployments.run_queued(queued.id)

    assert result.status is DeploymentStatus.SUCCEEDED
    assert set(runner.host_limits) == {"edge-a"}
    assert [target.edge for target in control.deployments.targets(queued.id)] == [
        "edge-a"
    ]


def test_removing_an_admitted_target_refuses_execution(settings):
    repository = Repository(settings.database_path)
    runner = FakeRunner()
    control = build_application(settings=settings, repository=repository, runner=runner)
    seed_edge(control, name="edge-a")
    seed_site(control)
    queued = control.deployments.submit_deployment("alice")
    repository.edges.delete_edge("edge-a")

    result = control.deployments.run_queued(queued.id)

    assert result.status is DeploymentStatus.FAILED
    assert "removed after admission" in result.detail
    assert runner.host_limits == []


@pytest.mark.parametrize("still_serving", [True, False])
def test_worker_resumes_checkpoints_and_reobserves_completed_edges(
    settings, still_serving
):
    repository = Repository(settings.database_path)
    runner = FakeRunner()
    verifier = FakeServingProbe(served=still_serving)
    control = build_application(
        settings=settings, repository=repository, runner=runner, serving_probe=verifier
    )
    seed_edge(control, name="edge-a")
    seed_edge(control, name="edge-b", host="192.0.2.11")
    seed_record(control)
    queued = control.deployments.submit_deployment("alice")
    repository.deployments.transition(
        queued.id, DeploymentStatus.QUEUED, DeploymentStatus.RUNNING
    )
    targets = repository.deployment_targets
    fence = targets.claim_generation(queued.id)
    targets.plan(queued.id, ["edge-a", "edge-b"], fence=fence)
    now = datetime.now(UTC)
    targets.begin(queued.id, "edge-a", fence=fence, now=now)
    for phase in PHASE_ORDER:
        targets.record_phase(queued.id, "edge-a", phase, fence=fence)
    targets.finish(
        queued.id, "edge-a", fence=fence, status=TargetStatus.SUCCEEDED, now=now
    )
    targets.begin(queued.id, "edge-b", fence=fence, now=now)
    targets.record_phase(queued.id, "edge-b", TargetPhase.VALIDATE, fence=fence)

    result = control.deployments.run_queued(queued.id)

    assert verifier.asked[0][0] == "edge-a"
    if still_serving:
        assert result.status is DeploymentStatus.SUCCEEDED
        assert runner.host_limits == ["edge-b", "edge-b", "edge-b"]
        assert runner.tag_selections == [("stage",), ("activate",), ("commit",)]
    else:
        assert result.status is DeploymentStatus.FAILED
        assert "recovery observation failed" in result.detail
        assert runner.host_limits == []
