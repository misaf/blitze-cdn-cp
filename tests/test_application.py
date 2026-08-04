import threading
import time
from contextlib import contextmanager

import pytest
from conftest import FakeRunner

from blitzecdn.application import ControlPlane
from blitzecdn.domain.models import CdnSite, DeploymentStatus, SitePatch
from blitzecdn.exceptions import ExecutionError, NotFoundError
from blitzecdn.infrastructure.ansible import CommandResult
from blitzecdn.infrastructure.database import Repository


def test_crud_validate_and_successful_deploy(settings, site_payload):
    repository = Repository(settings.database_path)
    runner = FakeRunner(
        [CommandResult(0, "syntax ok", ""), CommandResult(0, "applied", "")]
    )
    control = ControlPlane(settings, repository, runner)  # type: ignore[arg-type]
    site = control.create_site(CdnSite.model_validate(site_payload), "alice")
    control.update_site(site.name, SitePatch(cache_enabled=False), "alice")
    assert control.validate() == []
    result = control.deploy("alice")
    assert result.status is DeploymentStatus.SUCCEEDED
    assert result.stdout == "syntax ok"
    assert settings.generated_vars_path.exists()


def test_failed_and_timed_out_deployments_are_recorded(settings):
    repository = Repository(settings.database_path)
    runner = FakeRunner(
        [
            CommandResult(2, "", "failed"),
            CommandResult(124, "", "timeout", timed_out=True),
        ]
    )
    control = ControlPlane(settings, repository, runner)  # type: ignore[arg-type]
    assert control.deploy("alice").status is DeploymentStatus.FAILED
    assert control.deploy("alice", check=True).status is DeploymentStatus.TIMED_OUT
    assert runner.check_modes == [False, True]


def test_rollback_updates_canonical_state_only_after_success(settings, site_payload):
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings,
        repository,
        FakeRunner([CommandResult(0, "first", ""), CommandResult(0, "rollback", "")]),
    )  # type: ignore[arg-type]
    original = CdnSite.model_validate(site_payload)
    repository.create_site(original)
    successful = control.deploy("alice")
    repository.replace_site(original.model_copy(update={"origin_host": "192.0.2.99"}))
    result = control.rollback("alice", successful.id)
    assert result.status is DeploymentStatus.SUCCEEDED
    assert repository.get_site(original.name).origin_host == original.origin_host


def test_rollback_holds_the_lock_across_the_canonical_state_swap(
    settings, site_payload
):
    """Swapping sites after the lock released would drop concurrent edits."""
    events: list[str] = []
    repository = Repository(settings.database_path)

    class LockingRunner(FakeRunner):
        @contextmanager
        def lock(self):
            events.append("locked")
            try:
                yield
            finally:
                events.append("unlocked")

    original_replace = repository.replace_all_sites

    def recording_replace(sites):
        events.append("sites-replaced")
        original_replace(sites)

    repository.replace_all_sites = recording_replace  # type: ignore[method-assign]
    control = ControlPlane(
        settings,
        repository,
        LockingRunner([CommandResult(0, "first", ""), CommandResult(0, "back", "")]),
    )  # type: ignore[arg-type]
    original = CdnSite.model_validate(site_payload)
    repository.create_site(original)
    successful = control.deploy("alice")
    repository.replace_site(original.model_copy(update={"origin_host": "192.0.2.99"}))

    control.rollback("alice", successful.id)

    assert events == [
        "locked",
        "unlocked",  # the initial deploy
        "locked",
        "sites-replaced",
        "unlocked",
    ]


def test_submit_deployment_queues_and_converges_on_a_worker(settings, site_payload):
    repository = Repository(settings.database_path)
    release = threading.Event()

    class BlockingRunner(FakeRunner):
        def run(self, *, check):
            assert release.wait(timeout=5), "worker never reached the runner"
            return super().run(check=check)

    control = ControlPlane(settings, repository, BlockingRunner())  # type: ignore[arg-type]
    repository.create_site(CdnSite.model_validate(site_payload))

    queued = control.submit_deployment("alice")
    assert queued.status is DeploymentStatus.QUEUED

    release.set()
    assert _await_terminal(repository, queued.id) is DeploymentStatus.SUCCEEDED


def test_submit_rollback_reports_conflicts_synchronously(settings):
    """Nothing to roll back to must surface as an error, not a queued record."""
    repository = Repository(settings.database_path)
    control = ControlPlane(settings, repository, FakeRunner())  # type: ignore[arg-type]
    with pytest.raises(NotFoundError):
        control.submit_rollback("alice")


def test_submit_releases_the_lock_after_the_worker_finishes(settings, site_payload):
    repository = Repository(settings.database_path)
    control = ControlPlane(settings, repository, FakeRunner([CommandResult(0, "", "")]))  # type: ignore[arg-type]
    repository.create_site(CdnSite.model_validate(site_payload))

    queued = control.submit_deployment("alice")
    assert _await_terminal(repository, queued.id) is DeploymentStatus.SUCCEEDED
    # A second submission proves the worker handed the lock back.
    control.runner.results = [CommandResult(0, "", "")]
    again = control.submit_deployment("alice")
    assert _await_terminal(repository, again.id) is DeploymentStatus.SUCCEEDED


def test_runner_errors_are_recorded_and_reraised(settings, site_payload):
    class ExplodingRunner(FakeRunner):
        def run(self, *, check):
            raise ExecutionError("unable to execute Ansible")

    repository = Repository(settings.database_path)
    control = ControlPlane(settings, repository, ExplodingRunner())  # type: ignore[arg-type]
    repository.create_site(CdnSite.model_validate(site_payload))

    with pytest.raises(ExecutionError):
        control.deploy("alice")

    recorded = repository.list_deployments(1)[0]
    assert recorded.status is DeploymentStatus.FAILED
    assert "unable to execute Ansible" in recorded.stderr
    assert any(
        event.action == "deployment.failed"
        for event in repository.list_audit_events(10)
    )


def test_worker_survives_a_runner_error_and_releases_the_lock(settings, site_payload):
    """An exception on the worker thread must not strand the deployment lock."""
    repository = Repository(settings.database_path)
    calls: list[int] = []

    class ExplodingOnceRunner(FakeRunner):
        def run(self, *, check):
            calls.append(1)
            if len(calls) == 1:
                raise ExecutionError("boom")
            return CommandResult(0, "ok", "")

    control = ControlPlane(settings, repository, ExplodingOnceRunner())  # type: ignore[arg-type]
    repository.create_site(CdnSite.model_validate(site_payload))

    first = control.submit_deployment("alice")
    assert _await_terminal(repository, first.id) is DeploymentStatus.FAILED

    second = control.submit_deployment("alice")
    assert _await_terminal(repository, second.id) is DeploymentStatus.SUCCEEDED


def _await_terminal(
    repository: Repository, deployment_id: str, timeout: float = 5.0
) -> DeploymentStatus:
    deadline = time.monotonic() + timeout
    pending = {DeploymentStatus.QUEUED, DeploymentStatus.RUNNING}
    while time.monotonic() < deadline:
        status = repository.get_deployment(deployment_id).status
        if status not in pending:
            return status
        time.sleep(0.01)
    raise AssertionError(f"deployment {deployment_id} never finished")


def test_upload_and_request_certificate_activate_managed_tls(
    settings, site_payload, certificate_pair
):
    class FakeIssuer:
        def issue(self, site, email):
            assert site.name == "example-cdn"
            assert email == "owner@example.com"
            return certificate_pair()

    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings,
        repository,
        FakeRunner(),
        issuer=FakeIssuer(),
    )  # type: ignore[arg-type]
    repository.create_site(CdnSite.model_validate(site_payload))
    certificate, key = certificate_pair()

    uploaded = control.upload_certificate("example-cdn", certificate, key, "alice")
    assert uploaded.source == "uploaded"
    assert repository.get_site("example-cdn").certificate_mode == "uploaded"

    requested = control.request_certificate("example-cdn", "alice", "owner@example.com")
    assert requested.source == "acme"
    assert control.certificate("example-cdn") == requested
    assert repository.get_site("example-cdn").certificate_mode == "requested"

    result = control.deploy("alice", check=True)
    assert result.status is DeploymentStatus.SUCCEEDED
    desired = settings.generated_vars_path.read_text(encoding="utf-8")
    assert "certificate_source_path" in desired
    assert "PRIVATE KEY" not in desired


def test_request_certificate_requires_email(settings, site_payload):
    repository = Repository(settings.database_path)
    repository.create_site(CdnSite.model_validate(site_payload))
    control = ControlPlane(settings, repository, FakeRunner())  # type: ignore[arg-type]
    from blitzecdn.exceptions import ConflictError

    with pytest.raises(ConflictError, match="email"):
        control.request_certificate("example-cdn", "alice")


def test_certificate_upload_holds_deployment_lock(
    settings, site_payload, certificate_pair
):
    events: list[str] = []

    class LockingRunner(FakeRunner):
        @contextmanager
        def lock(self):
            events.append("locked")
            try:
                yield
            finally:
                events.append("unlocked")

    class RecordingStore:
        def install(self, site, certificate, key, *, source, email=None):
            assert events == ["locked"]
            events.append("installed")
            from blitzecdn.infrastructure.certificates import CertificateStore

            return CertificateStore(settings).install(
                site, certificate, key, source=source, email=email
            )

    repository = Repository(settings.database_path)
    repository.create_site(CdnSite.model_validate(site_payload))
    control = ControlPlane(
        settings,
        repository,
        LockingRunner(),
        certificate_store=RecordingStore(),  # type: ignore[arg-type]
    )
    certificate, key = certificate_pair()

    control.upload_certificate("example-cdn", certificate, key, "alice")

    assert events == ["locked", "installed", "unlocked"]
