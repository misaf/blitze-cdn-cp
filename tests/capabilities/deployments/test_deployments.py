# ruff: noqa: F403,F405
from application_support import *


def _await_terminal(
    repository: Repository, deployment_id: str, timeout: float = 5.0
) -> DeploymentStatus:
    deadline = time.monotonic() + timeout
    pending = {DeploymentStatus.QUEUED, DeploymentStatus.RUNNING}
    while time.monotonic() < deadline:
        status = repository.deployments.get_deployment(deployment_id).status
        if status not in pending:
            return status
        time.sleep(0.01)
    raise AssertionError(f"deployment {deployment_id} never finished")


def _await_workflow(
    repository: Repository, resource_id: str, timeout: float = 5.0
) -> WorkflowStatus:
    """Wait for the workflow covering a queued run to close.

    A separate wait from `_await_terminal`: the deployment reaches a terminal
    status inside the convergence, and the workflow closes around it, so the
    two finish in that order and asserting on the second right after the first
    is a race.
    """
    deadline = time.monotonic() + timeout
    pending = {WorkflowStatus.PENDING, WorkflowStatus.RUNNING}
    while time.monotonic() < deadline:
        for workflow in repository.workflows.list_workflows(10):
            if workflow.resource_id == resource_id and workflow.status not in pending:
                return workflow.status
        time.sleep(0.01)
    raise AssertionError(f"no workflow for {resource_id} finished")


def test_a_canary_records_its_limit_and_passes_it_to_ansible(settings):
    repository = Repository(settings.database_path)
    runner = FakeRunner([ansible_run(host_run("edge-a"))])
    control = ControlPlane(settings=settings, repository=repository, runner=runner)  # type: ignore[arg-type]
    seed_site(control)

    result = control.deployments.deploy("alice", host_limit=" edge-a ")

    assert result.host_limit == "edge-a", "the limit is normalised before storage"
    assert runner.host_limits == ["edge-a"]


def test_a_canary_is_never_the_automatic_rollback_target(settings):
    """A limited run only proves one edge reached that snapshot.

    Rolling the fleet back to it would converge every other edge onto a state
    it had never been given, which is the disagreement rollback exists to end.
    """
    repository = Repository(settings.database_path)
    runner = FakeRunner([ansible_run(host_run("edge-a")) for _ in range(3)])
    control = ControlPlane(settings=settings, repository=repository, runner=runner)  # type: ignore[arg-type]

    # Three distinct desired states. A snapshot carries the zones, the records
    # and the sites, so any of the three produces a different one.
    seed_site(control, name="cdn-example-com", record="cdn")
    full = control.deployments.deploy("alice")

    repository.zones.delete_record("example.com", "cdn", RecordType.A)
    canary = control.deployments.deploy("alice", host_limit="edge-a")
    assert canary.status is DeploymentStatus.SUCCEEDED

    # A third, distinct state, so both earlier snapshots are eligible and the
    # canary is the more recent of the two. Without the filter it would win.
    seed_site(control, name="other-example-com", record="other")
    assert (
        repository.deployments.successful_rollback_target(repository.snapshot()).id
        == full.id
    )


def test_a_malformed_limit_is_refused_before_a_deployment_is_recorded(settings):
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings, repository=repository, runner=FakeRunner()
    )  # type: ignore[arg-type]
    seed_site(control)

    with pytest.raises(ValueError, match="only narrow a deploy"):
        control.deployments.deploy("alice", host_limit="edge-a:!edge-b")

    assert repository.deployments.list_deployments(5) == []


def _in_sync_run():
    return ansible_run(host_run("edge-a"), host_run("edge-b"))


def _drifted_run():
    """edge-a would rewrite two vhosts and reload; edge-b is converged.

    The task names matter now: a drift report says which configuration moved,
    not only how many tasks would run.
    """
    return ansible_run(
        host_run(
            "edge-a",
            changes=("Render managed sites", "Enable desired sites", "Reload Nginx"),
        ),
        host_run("edge-b"),
    )


def test_drift_check_runs_without_changing_anything(settings):
    repository = Repository(settings.database_path)
    runner = FakeRunner([_in_sync_run()])
    control = ControlPlane(settings=settings, repository=repository, runner=runner)  # type: ignore[arg-type]
    seed_site(control)

    report = control.deployments.check_drift("alice")

    assert runner.check_modes == [True], "a drift check must never apply changes"
    assert report.in_sync is True
    assert report.drifted == ()


def test_drift_check_names_the_edges_that_moved(settings):
    repository = Repository(settings.database_path)
    runner = FakeRunner([_drifted_run()])
    control = ControlPlane(settings=settings, repository=repository, runner=runner)  # type: ignore[arg-type]
    seed_site(control)

    report = control.deployments.check_drift("alice")

    assert report.in_sync is False
    assert [host.host for host in report.drifted] == ["edge-a"]
    assert any(
        event.action == "drift.checked" and event.details["drifted"] == ["edge-a"]
        for event in repository.audit_log.list_audit_events(10)
    )


def test_a_drift_report_can_be_reread_from_the_recorded_deployment(settings):
    repository = Repository(settings.database_path)
    runner = FakeRunner([_drifted_run()])
    control = ControlPlane(settings=settings, repository=repository, runner=runner)  # type: ignore[arg-type]
    seed_site(control)

    first = control.deployments.check_drift("alice")
    again = control.deployments.drift_report(first.deployment_id)

    assert again.hosts == first.hosts


def test_an_applied_deployment_is_not_a_drift_report(settings):
    """Its output says what it did, not what had drifted."""
    repository = Repository(settings.database_path)
    runner = FakeRunner([_drifted_run()])
    control = ControlPlane(settings=settings, repository=repository, runner=runner)  # type: ignore[arg-type]
    seed_site(control)

    applied = control.deployments.deploy("alice")
    with pytest.raises(ConflictError, match="applied changes"):
        control.deployments.drift_report(applied.id)


class _RecordingIssuer:
    """Stands in for certbot: hands back a fresh pair and remembers the call."""

    def __init__(self, certificate_pair, *, fails: set[str] | None = None) -> None:
        self._pair = certificate_pair
        self._fails = fails or set()
        self.issued: list[tuple[str, str]] = []

    def issue(self, site, email):
        if site.name in self._fails:
            raise ExecutionError("challenge failed")
        self.issued.append((site.name, email))
        return self._pair((site.server_names[0],), days=90)


def _proxied_site_with_certificate(control, repository, certificate_pair, *, days):
    site = _seed_proxied_record(control)
    certificate, key = certificate_pair((site.server_names[0],), days=days)
    return control.certificates.upload_certificate(site.name, certificate, key, "alice")
