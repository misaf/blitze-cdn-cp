import threading
import time
from contextlib import contextmanager

import pytest
from conftest import FakeRunner

from blitzecdn.application import ControlPlane
from blitzecdn.domain.models import (
    CdnSite,
    CertificateMode,
    DeploymentStatus,
    DnsRecord,
    Domain,
    RecordPatch,
    RecordType,
)
from blitzecdn.exceptions import ConflictError, ExecutionError, NotFoundError
from blitzecdn.infrastructure.ansible import CommandResult
from blitzecdn.infrastructure.database import Repository


def _seed_proxied_record(control: ControlPlane) -> DnsRecord:
    """Create the one zone and proxied record most tests need.

    Sites can no longer be inserted directly — proxying a record is the only
    way one comes into existence — so this is the shared setup for anything
    that needs `cdn-example-com` to exist.
    """
    control.create_domain(Domain(name="example.com"), "alice")
    return control.create_record(
        DnsRecord(
            domain="example.com", name="cdn", value="198.51.100.10", proxied=True
        ),
        "alice",
    )


def test_crud_validate_and_successful_deploy(settings):
    repository = Repository(settings.database_path)
    runner = FakeRunner(
        [CommandResult(0, "syntax ok", ""), CommandResult(0, "applied", "")]
    )
    control = ControlPlane(settings, repository, runner)  # type: ignore[arg-type]
    control.create_domain(Domain(name="example.com"), "alice")
    record = control.create_record(
        DnsRecord(
            domain="example.com", name="cdn", value="198.51.100.10", proxied=True
        ),
        "alice",
    )
    control.update_record(
        "example.com", "cdn", RecordType.A, RecordPatch(cache_enabled=False), "alice"
    )
    assert repository.get_site(record.site_name).cache_enabled is False
    assert control.validate() == []
    result = control.deploy("alice")
    assert result.status is DeploymentStatus.SUCCEEDED
    assert result.stdout == "syntax ok"
    assert settings.generated_vars_path.exists()


def test_proxy_toggle_adds_and_removes_the_edge_virtual_host(settings):
    """The CDN on/off switch is what decides whether the edge serves a name."""
    repository = Repository(settings.database_path)
    control = ControlPlane(settings, repository, FakeRunner())  # type: ignore[arg-type]
    control.create_domain(Domain(name="example.com"), "alice")
    control.create_record(
        DnsRecord(
            domain="example.com", name="cdn", value="198.51.100.10", proxied=True
        ),
        "alice",
    )
    control.create_record(
        DnsRecord(domain="example.com", name="db", value="198.51.100.11"), "alice"
    )

    # Only the proxied record reaches the edge.
    assert [site.server_names[0] for site in repository.list_sites()] == [
        "cdn.example.com"
    ]

    control.set_proxied("example.com", "cdn", RecordType.A, False, "alice")
    assert repository.list_sites() == []

    control.set_proxied("example.com", "cdn", RecordType.A, True, "alice")
    assert [site.server_names[0] for site in repository.list_sites()] == [
        "cdn.example.com"
    ]


def test_removing_a_domain_takes_its_virtual_hosts_off_the_edge(settings):
    repository = Repository(settings.database_path)
    control = ControlPlane(settings, repository, FakeRunner())  # type: ignore[arg-type]
    control.create_domain(Domain(name="example.com"), "alice")
    control.create_record(
        DnsRecord(
            domain="example.com", name="cdn", value="198.51.100.10", proxied=True
        ),
        "alice",
    )
    control.delete_domain("example.com", "alice")
    assert repository.list_records() == []
    assert repository.list_sites() == []


def test_records_that_collide_on_a_derived_site_name_are_refused(settings):
    """'a.b.example.com' and 'a-b.example.com' both flatten to a-b-example-com."""
    repository = Repository(settings.database_path)
    control = ControlPlane(settings, repository, FakeRunner())  # type: ignore[arg-type]
    control.create_domain(Domain(name="example.com"), "alice")
    control.create_record(
        DnsRecord(
            domain="example.com", name="a.b", value="198.51.100.10", proxied=True
        ),
        "alice",
    )
    with pytest.raises(ConflictError, match="internal site name"):
        control.create_record(
            DnsRecord(
                domain="example.com", name="a-b", value="198.51.100.11", proxied=True
            ),
            "alice",
        )


def test_validate_reports_a_collision_that_bypassed_the_create_check(settings):
    """Backstop for records restored from a snapshot rather than created."""
    repository = Repository(settings.database_path)
    control = ControlPlane(settings, repository, FakeRunner())  # type: ignore[arg-type]
    control.create_domain(Domain(name="example.com"), "alice")
    for label in ("a.b", "a-b"):
        repository.create_record(
            DnsRecord(
                domain="example.com", name=label, value="198.51.100.10", proxied=True
            )
        )
    assert any("derive the internal site name" in error for error in control.validate())
    # Deriving must survive the collision rather than fail to write. Any record
    # change triggers the re-derivation, so use one to exercise that path.
    control.create_record(
        DnsRecord(
            domain="example.com", name="www", value="198.51.100.12", proxied=True
        ),
        "alice",
    )
    assert sorted(site.name for site in repository.list_sites()) == [
        "a-b-example-com",
        "www-example-com",
    ]


def test_validate_rejects_acme_on_a_reserved_domain(settings):
    """No public CA issues for .test, so catch it before certbot is invoked."""
    repository = Repository(settings.database_path)
    control = ControlPlane(settings, repository, FakeRunner())  # type: ignore[arg-type]
    control.create_domain(Domain(name="vendra.test"), "alice")
    control.create_record(
        DnsRecord(
            domain="vendra.test",
            name="api",
            value="198.51.100.10",
            proxied=True,
            certificate_mode=CertificateMode.REQUESTED,
            certificate_path="/etc/blitzecdn/tls/api-vendra-test/fullchain.pem",
            certificate_key_path="/etc/blitzecdn/tls/api-vendra-test/privkey.pem",
        ),
        "alice",
    )
    assert any("reserved name" in error for error in control.validate())


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
    control.create_domain(Domain(name="example.com"), "alice")
    original = control.create_record(
        DnsRecord(
            domain="example.com", name="cdn", value="198.51.100.10", proxied=True
        ),
        "alice",
    )
    successful = control.deploy("alice")
    control.update_record(
        "example.com", "cdn", RecordType.A, RecordPatch(value="192.0.2.99"), "alice"
    )
    result = control.rollback("alice", successful.id)
    assert result.status is DeploymentStatus.SUCCEEDED
    # Rollback restores the record, and the derived site follows from it.
    restored = repository.get_record("example.com", "cdn", RecordType.A)
    assert restored.value == original.value
    assert repository.get_site(original.site_name).origin_host == original.value


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
            assert site.name == "cdn-example-com"
            assert email == "owner@example.com"
            return certificate_pair()

    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings,
        repository,
        FakeRunner(),
        issuer=FakeIssuer(),
    )  # type: ignore[arg-type]
    _seed_proxied_record(control)
    certificate, key = certificate_pair()

    uploaded = control.upload_certificate("cdn-example-com", certificate, key, "alice")
    assert uploaded.source == "uploaded"
    assert repository.get_site("cdn-example-com").certificate_mode == "uploaded"

    requested = control.request_certificate(
        "cdn-example-com", "alice", "owner@example.com"
    )
    assert requested.source == "acme"
    assert control.certificate("cdn-example-com") == requested
    assert repository.get_site("cdn-example-com").certificate_mode == "requested"

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
        control.request_certificate("cdn-example-com", "alice")


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
    control = ControlPlane(
        settings,
        repository,
        LockingRunner(),
        certificate_store=RecordingStore(),  # type: ignore[arg-type]
    )
    _seed_proxied_record(control)
    events.clear()  # seeding does not take the deployment lock
    certificate, key = certificate_pair()

    control.upload_certificate("cdn-example-com", certificate, key, "alice")

    assert events == ["locked", "installed", "unlocked"]
