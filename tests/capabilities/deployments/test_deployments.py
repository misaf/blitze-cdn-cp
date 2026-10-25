import pytest
from support.application import _seed_proxied_record
from support.control_plane import (
    FakeRunner,
    advance,
    ansible_run,
    build_application,
    host_run,
    seed_edge,
    seed_site,
)

from blitzecdn.capabilities.deployments.domain import DeploymentStatus
from blitzecdn.capabilities.dns.domain import RecordType
from blitzecdn.composition import Repository
from blitzecdn.core.exceptions import ConflictError, ExecutionError


def test_a_canary_records_its_limit_and_converges_only_that_edge(settings):
    """A limit narrows the rollout, not just the `--limit` Ansible is given.

    An edge outside it is never begun, and a row says so.
    """
    repository = Repository(settings.database_path)
    runner = FakeRunner([ansible_run(host_run("edge-a"))])
    control = build_application(settings=settings, repository=repository, runner=runner)
    seed_edge(control, name="edge-a")
    seed_edge(control, name="edge-b", host="192.0.2.11")
    seed_site(control)

    result = control.deployments.deploy("alice", host_limit=" edge-a ")

    assert result.host_limit == "edge-a", "the limit is normalised before storage"
    assert set(runner.host_limits) == {"edge-a"}
    assert [
        target.edge for target in repository.deployment_targets.targets(result.id)
    ] == ["edge-a"]


def test_a_canary_is_never_the_automatic_rollback_target(settings):
    """A limited run only proves one edge reached that release."""
    repository = Repository(settings.database_path)
    runner = FakeRunner([ansible_run(host_run("edge-a")) for _ in range(3)])
    control = build_application(settings=settings, repository=repository, runner=runner)

    # Three distinct desired states, so three release identifiers.
    seed_edge(control, name="edge-a")
    seed_site(control, name="example-com", record="cdn")
    full = control.deployments.deploy("alice")

    repository.zones.delete_record("example.com", "cdn", RecordType.A)
    canary = control.deployments.deploy("alice", host_limit="edge-a")
    assert canary.status is DeploymentStatus.SUCCEEDED

    # A third, distinct state, so both earlier releases are eligible and the
    # canary is the more recent of the two. Without the filter it would win.
    seed_site(control, name="other-example-com", record="other", always_use_https=True)
    current = control.releases.compile().id
    assert repository.deployments.successful_rollback_target(current).id == full.id


def test_a_malformed_limit_is_refused_before_a_deployment_is_recorded(settings):
    repository = Repository(settings.database_path)
    control = build_application(
        settings=settings, repository=repository, runner=FakeRunner()
    )
    seed_site(control)

    with pytest.raises(ValueError, match="only narrow a deploy"):
        control.deployments.deploy("alice", host_limit="edge-a:!edge-b")

    assert repository.deployments.list_deployments(5) == []


@pytest.mark.parametrize(
    ("check_mode", "status"),
    [
        (True, DeploymentStatus.SUCCEEDED),
        (True, DeploymentStatus.FAILED),
        (False, DeploymentStatus.FAILED),
        (False, DeploymentStatus.QUEUED),
        (False, DeploymentStatus.RUNNING),
    ],
)
def test_newer_non_applied_runs_do_not_hide_a_deployed_site(
    settings, check_mode, status
):
    repository = Repository(settings.database_path)
    control = build_application(
        settings=settings, repository=repository, runner=FakeRunner()
    )
    seed_edge(control, name="edge-a")
    seed_site(control)
    applied = control.deployments.deploy("alice", host_limit="edge-a")
    assert applied.status is DeploymentStatus.SUCCEEDED

    with repository.transaction():
        for _ in range(51):
            newer = repository.deployments.create_deployment(
                "alice", release_id=applied.release_id, check_mode=check_mode
            )
            if status is DeploymentStatus.QUEUED:
                continue
            advance(repository.deployments, newer.id, DeploymentStatus.RUNNING)
            if status is not DeploymentStatus.RUNNING:
                advance(repository.deployments, newer.id, status)

    assert control.deployments.site_is_deployed("example-com") is True
    assert control.deployments.site_is_deployed("missing-site") is False


def test_site_lookup_uses_only_the_newest_successful_applied_release(settings):
    repository = Repository(settings.database_path)
    control = build_application(
        settings=settings, repository=repository, runner=FakeRunner()
    )
    seed_edge(control, name="edge-a")
    seed_site(control)
    control.deployments.deploy("alice")
    repository.zones.delete_record("example.com", "cdn", RecordType.A)

    removed = control.deployments.deploy("alice")

    assert removed.status is DeploymentStatus.SUCCEEDED
    assert control.deployments.site_is_deployed("example-com") is False


def test_latest_successful_applied_is_absent_on_an_empty_controller(settings):
    repository = Repository(settings.database_path)

    assert repository.deployments.latest_successful_applied() is None


def _in_sync_run():
    return ansible_run(host_run("edge-a"), host_run("edge-b"))


def _drifted_run():
    """edge-a would rewrite two vhosts and reload; edge-b is converged."""
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
    control = build_application(settings=settings, repository=repository, runner=runner)
    seed_edge(control, name="edge-a")
    seed_site(control)

    report = control.deployments.check_drift("alice")

    assert runner.check_modes == [True], "a drift check must never apply changes"
    assert runner.tag_selections == [()], "and never stages an image either"
    assert report.in_sync is True
    assert report.drifted == ()


def test_drift_check_names_the_edges_that_moved(settings):
    repository = Repository(settings.database_path)
    runner = FakeRunner([_drifted_run()])
    control = build_application(settings=settings, repository=repository, runner=runner)
    seed_edge(control, name="edge-a")
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
    control = build_application(settings=settings, repository=repository, runner=runner)
    seed_site(control)

    first = control.deployments.check_drift("alice")
    again = control.deployments.drift_report(first.deployment_id)

    assert again.hosts == first.hosts


def test_an_applied_deployment_is_not_a_drift_report(settings):
    """Its output says what it did, not what had drifted."""
    repository = Repository(settings.database_path)
    runner = FakeRunner([_drifted_run()])
    control = build_application(settings=settings, repository=repository, runner=runner)
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


def test_a_transition_is_a_new_value_and_keeps_what_it_did_not_change():
    """`start` and `finish` return the next state; the original is untouched."""
    from datetime import UTC, datetime, timedelta

    from blitzecdn.capabilities.deployments.domain import Deployment

    created = datetime(2030, 1, 1, tzinfo=UTC)
    queued = Deployment(
        id="d" * 32,
        status=DeploymentStatus.QUEUED,
        operator="alice",
        check_mode=False,
        release_id="r1",
        created_at=created,
    )
    running = queued.start(now=created + timedelta(seconds=1))
    failed = running.finish(
        DeploymentStatus.FAILED,
        now=created + timedelta(seconds=9),
        error="edge-b did not answer",
    )

    assert queued.status is DeploymentStatus.QUEUED
    assert running.started_at == created + timedelta(seconds=1)
    assert failed.started_at == running.started_at
    assert failed.finished_at == created + timedelta(seconds=9)
    assert failed.detail == "edge-b did not answer"


def test_converging_requirements_queues_one_deployment_and_only_when_owed(
    settings,
):
    from support.control_plane import FakeRunner, build_application, seed_site

    from blitzecdn.capabilities.deployments.domain import DeploymentRequirementKind
    from blitzecdn.composition import Repository

    repository = Repository(settings.database_path)
    control = build_application(
        settings=settings, repository=repository, runner=FakeRunner()
    )
    seed_site(control)
    try:
        assert control.deployments.converge_requirements("timer") is None

        repository.deployment_requirements.require(
            DeploymentRequirementKind.CERTIFICATES
        )
        queued = control.deployments.converge_requirements("timer")
        assert queued is not None
        assert queued.status is DeploymentStatus.QUEUED
        # Still owed until a deployment succeeds, but already being paid off.
        assert control.deployments.converge_requirements("timer").id == queued.id
        assert len(repository.deployments.queued_deployments()) == 1
    finally:
        control.close()
