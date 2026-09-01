import time
from contextlib import contextmanager
from dataclasses import replace
from importlib import import_module
from importlib.util import find_spec

import pytest
from control_plane_fixtures import (
    FakePreflight,
    FakeRunner,
    RecordingBackgroundQueue,
    ansible_run,
    host_run,
    origin_report,
)

from blitzecdn.bootstrap import ControlPlane
from blitzecdn.core.database import Repository
from blitzecdn.core.exceptions import (
    ConflictError,
    DeploymentBusyError,
)
from blitzecdn.core.operations import WorkflowKind, WorkflowStatus
from blitzecdn.core.runs import HostRun, RunStatus
from blitzecdn.features.deployments.domain import DeploymentStatus
from blitzecdn.features.dns.domain import DnsRecord, Domain, RecordPatch, RecordType
from blitzecdn.features.sites.domain import CdnSite
from blitzecdn.features.tls.policy import CertificateMode, SslAutomaticMode, SslMode

CertificateSource = (
    import_module("blitzecdn_certificates.certificates.domain").CertificateSource
    if find_spec("blitzecdn_certificates") is not None
    else None
)

REQUIRES_CERTIFICATES = frozenset(
    {
        "test_validate_rejects_acme_on_a_reserved_domain",
        "test_busy_external_work_does_not_create_a_false_workflow",
        "test_a_renewal_blocked_by_a_deployment_is_skipped_not_failed",
        "test_an_interrupted_issuance_says_how_far_it_got",
    }
)


def _seed_proxied_record(control: ControlPlane) -> DnsRecord:
    """Create the one zone and proxied record most tests need.

    Sites can no longer be inserted directly — proxying a record is the only
    way one comes into existence — so this is the shared setup for anything
    that needs `cdn-example-com` to exist.
    """
    control.dns.create_domain(Domain(name="example.com"), "alice")
    return control.dns.create_record(
        DnsRecord(
            domain="example.com", name="cdn", value="198.51.100.10", proxied=True
        ),
        "alice",
    )


def _automatic_origin_report(
    mode: SslMode,
    *,
    reachable: bool = True,
    tls_verified: bool | None = None,
    status: int = 200,
) -> HostRun:
    scheme = "https" if mode in {SslMode.FULL, SslMode.FULL_STRICT} else "http"
    return host_run(
        "edge-a",
        report={
            "host": "edge-a",
            "collected_at": "2026-01-01T00:00:00Z",
            "origins": [
                {
                    "site": "cdn-example-com",
                    "origin": f"198.51.100.10:{443 if scheme == 'https' else 80}",
                    "scheme": scheme,
                    "ssl_mode": mode.value,
                    "sni": "198.51.100.10" if scheme == "https" else None,
                    "reachable": str(reachable),
                    "tls_verified": (
                        "None" if tls_verified is None else str(tls_verified)
                    ),
                    "status": str(status) if reachable else "-1",
                    "content_sha256": "a" * 64 if reachable else None,
                    "detail": "",
                }
            ],
        },
    )


def _seed_automatic_ssl_record(
    control: ControlPlane,
    *,
    mode: SslMode = SslMode.OFF,
    automatic: SslAutomaticMode = SslAutomaticMode.AUTO,
) -> None:
    control.dns.create_domain(Domain(name="example.com"), "alice")
    control.dns.create_record(
        DnsRecord.model_validate(
            {
                "domain": "example.com",
                "name": "cdn",
                "value": "198.51.100.10",
                "proxied": True,
                "ssl_mode": mode,
                "ssl_automatic_mode": automatic,
                "certificate_mode": "existing",
                "certificate_path": "/etc/ssl/certs/edge.pem",
                "certificate_key_path": "/etc/ssl/private/edge.key",
            }
        ),
        "alice",
    )


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
    deadline = time.monotonic() + timeout
    pending = {WorkflowStatus.PENDING, WorkflowStatus.RUNNING}
    while time.monotonic() < deadline:
        for workflow in repository.workflows.list_workflows(10):
            if workflow.resource_id == resource_id and workflow.status not in pending:
                return workflow.status
        time.sleep(0.01)
    raise AssertionError(f"no workflow for {resource_id} finished")


def test_control_plane_closes_only_the_repository_it_owns(settings, monkeypatch):
    closed: list[Repository] = []
    monkeypatch.setattr(
        Repository, "close", lambda repository: closed.append(repository)
    )

    owned = ControlPlane(settings=settings)
    owned.close()
    owned.close()

    injected_repository = Repository(settings.database_path)
    injected = ControlPlane(settings=settings, repository=injected_repository)
    injected.close()

    assert len(closed) == 1


def test_dns_write_projection_and_audit_are_one_transaction(settings, monkeypatch):
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner(),  # type: ignore[arg-type]
    )
    control.dns.create_domain(Domain(name="example.com"), "alice")

    def refuse_event(_event):
        raise RuntimeError("audit recorder failed")

    # Event recording participates in the same unit of work as the record and
    # derived-site writes, so any recorder failure must roll everything back.
    monkeypatch.setattr(repository.audit_log, "record", refuse_event)
    with pytest.raises(RuntimeError, match="audit recorder failed"):
        control.dns.create_record(
            DnsRecord(
                domain="example.com",
                name="cdn",
                value="198.51.100.10",
                proxied=True,
            ),
            "alice",
        )

    assert repository.zones.list_records() == []
    assert repository.sites.list_sites() == []
    assert [event.action for event in repository.audit_log.list_audit_events()] == [
        "domain.created"
    ]


def test_projection_drift_is_detected_and_repairable(settings):
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings, repository=repository, runner=FakeRunner()
    )  # type: ignore[arg-type]
    record = _seed_proxied_record(control)
    repository.sites.replace_all_sites([])

    assert control.dns.validation_errors() == [
        "the site projection is stale; rebuild it before deploying"
    ]
    control.dns.rebuild_site_projection()

    assert control.dns.validation_errors() == []
    assert repository.sites.get_site(record.site_name).origin_host == record.value


def test_external_deployment_run_never_holds_a_database_transaction(settings):
    repository = Repository(settings.database_path)

    class TransactionAwareRunner(FakeRunner):
        def run(self, *, check: bool, host_limit: str | None = None):
            assert getattr(repository.database._local, "connection", None) is None
            return super().run(check=check, host_limit=host_limit)

    control = ControlPlane(
        settings=settings, repository=repository, runner=TransactionAwareRunner()
    )  # type: ignore[arg-type]
    control.deployments.deploy("alice")


def test_crud_validate_and_successful_deploy(settings):
    repository = Repository(settings.database_path)
    runner = FakeRunner([ansible_run(host_run("edge-a")) for _ in range(2)])
    control = ControlPlane(settings=settings, repository=repository, runner=runner)  # type: ignore[arg-type]
    control.dns.create_domain(Domain(name="example.com"), "alice")
    record = control.dns.create_record(
        DnsRecord(
            domain="example.com", name="cdn", value="198.51.100.10", proxied=True
        ),
        "alice",
    )
    control.dns.update_record(
        "example.com",
        "cdn",
        RecordType.A,
        RecordPatch(cache_enabled=False, compression="off"),
        "alice",
    )
    assert repository.sites.get_site(record.site_name).cache_enabled is False
    assert control.deployments.validate() == []
    result = control.deployments.deploy("alice")
    assert result.status is DeploymentStatus.SUCCEEDED
    assert result.result is not None
    assert [host.host for host in result.hosts] == ["edge-a"]
    assert settings.generated_vars_path.exists()


def test_desired_state_requires_explicit_approval_to_remove_all_sites(settings):
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings, repository=repository, runner=FakeRunner()
    )  # type: ignore[arg-type]

    assert control.deployments.deploy("alice").status is DeploymentStatus.SUCCEEDED
    desired = settings.generated_vars_path.read_text(encoding="utf-8")
    assert "blitzecdn_nginx_allow_empty_sites: false" in desired

    approved = ControlPlane(
        settings=settings.model_copy(update={"allow_empty_sites": True}),
        repository=repository,
        runner=FakeRunner(),
    )  # type: ignore[arg-type]
    assert approved.deployments.deploy("alice").status is DeploymentStatus.SUCCEEDED
    desired = settings.generated_vars_path.read_text(encoding="utf-8")
    assert "blitzecdn_nginx_allow_empty_sites: true" in desired


def test_interrupted_deployment_is_recorded_as_abandoned(settings):
    class InterruptedRunner(FakeRunner):
        def run(self, *, check: bool, host_limit: str | None = None):
            raise KeyboardInterrupt

    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings, repository=repository, runner=InterruptedRunner()
    )  # type: ignore[arg-type]

    with pytest.raises(KeyboardInterrupt):
        control.deployments.deploy("alice")

    deployment = repository.deployments.list_deployments(1)[0]
    assert deployment.status is DeploymentStatus.ABANDONED
    assert deployment.finished_at is not None
    assert deployment.result is not None
    assert "KeyboardInterrupt" in (deployment.result.error or "")


def test_proxy_toggle_adds_and_removes_the_edge_virtual_host(settings):
    """The CDN on/off switch is what decides whether the edge serves a name."""
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings, repository=repository, runner=FakeRunner()
    )  # type: ignore[arg-type]
    control.dns.create_domain(Domain(name="example.com"), "alice")
    control.dns.create_record(
        DnsRecord(
            domain="example.com", name="cdn", value="198.51.100.10", proxied=True
        ),
        "alice",
    )
    control.dns.create_record(
        DnsRecord(domain="example.com", name="db", value="198.51.100.11"), "alice"
    )

    # Only the proxied record reaches the edge.
    assert [site.server_names[0] for site in repository.sites.list_sites()] == [
        "cdn.example.com"
    ]

    control.dns.set_proxied("example.com", "cdn", RecordType.A, False, "alice")
    assert repository.sites.list_sites() == []

    control.dns.set_proxied("example.com", "cdn", RecordType.A, True, "alice")
    assert [site.server_names[0] for site in repository.sites.list_sites()] == [
        "cdn.example.com"
    ]


def test_removing_a_domain_takes_its_virtual_hosts_off_the_edge(settings):
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings, repository=repository, runner=FakeRunner()
    )  # type: ignore[arg-type]
    control.dns.create_domain(Domain(name="example.com"), "alice")
    control.dns.create_record(
        DnsRecord(
            domain="example.com", name="cdn", value="198.51.100.10", proxied=True
        ),
        "alice",
    )
    control.dns.delete_domain("example.com", "alice")
    assert repository.zones.list_records() == []
    assert repository.sites.list_sites() == []


def test_records_that_collide_on_a_derived_site_name_are_refused(settings):
    """'a.b.example.com' and 'a-b.example.com' both flatten to a-b-example-com."""
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings, repository=repository, runner=FakeRunner()
    )  # type: ignore[arg-type]
    control.dns.create_domain(Domain(name="example.com"), "alice")
    control.dns.create_record(
        DnsRecord(
            domain="example.com", name="a.b", value="198.51.100.10", proxied=True
        ),
        "alice",
    )
    with pytest.raises(ConflictError, match="internal site name"):
        control.dns.create_record(
            DnsRecord(
                domain="example.com", name="a-b", value="198.51.100.11", proxied=True
            ),
            "alice",
        )


def test_validate_reports_a_collision_that_bypassed_the_create_check(settings):
    """Backstop for records restored from a snapshot rather than created."""
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings, repository=repository, runner=FakeRunner()
    )  # type: ignore[arg-type]
    control.dns.create_domain(Domain(name="example.com"), "alice")
    for label in ("a.b", "a-b"):
        repository.zones.create_record(
            DnsRecord(
                domain="example.com", name=label, value="198.51.100.10", proxied=True
            )
        )
    assert any(
        "derive the internal site name" in error
        for error in control.deployments.validate()
    )
    # Deriving must survive the collision rather than fail to write. Any record
    # change triggers the re-derivation, so use one to exercise that path.
    control.dns.create_record(
        DnsRecord(
            domain="example.com", name="www", value="198.51.100.12", proxied=True
        ),
        "alice",
    )
    assert sorted(site.name for site in repository.sites.list_sites()) == [
        "a-b-example-com",
        "www-example-com",
    ]


def test_validate_rejects_acme_on_a_reserved_domain(settings):
    """No public CA issues for .test, so catch it before certbot is invoked."""
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings, repository=repository, runner=FakeRunner()
    )  # type: ignore[arg-type]
    control.dns.create_domain(Domain(name="vendra.test"), "alice")
    control.dns.create_record(
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
    assert any("reserved name" in error for error in control.deployments.validate())


def test_failed_and_timed_out_deployments_are_recorded(settings):
    repository = Repository(settings.database_path)
    runner = FakeRunner(
        [
            ansible_run(
                host_run("edge-a", failure="nginx -t rejected the configuration"),
                status=RunStatus.FAILED,
                return_code=2,
            ),
            ansible_run(status=RunStatus.TIMED_OUT, return_code=124),
        ]
    )
    control = ControlPlane(settings=settings, repository=repository, runner=runner)  # type: ignore[arg-type]
    assert control.deployments.deploy("alice").status is DeploymentStatus.FAILED
    assert (
        control.deployments.deploy("alice", check=True).status
        is DeploymentStatus.TIMED_OUT
    )
    assert runner.check_modes == [False, True]


def test_rollback_updates_canonical_state_only_after_success(settings, site_payload):
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner([ansible_run(host_run("edge-a")) for _ in range(2)]),
    )  # type: ignore[arg-type]
    control.dns.create_domain(Domain(name="example.com"), "alice")
    original = control.dns.create_record(
        DnsRecord(
            domain="example.com", name="cdn", value="198.51.100.10", proxied=True
        ),
        "alice",
    )
    successful = control.deployments.deploy("alice")
    control.dns.update_record(
        "example.com", "cdn", RecordType.A, RecordPatch(value="192.0.2.99"), "alice"
    )
    result = control.deployments.rollback("alice", successful.id)
    assert result.status is DeploymentStatus.SUCCEEDED
    # Rollback restores the record, and the derived site follows from it.
    restored = repository.zones.get_record("example.com", "cdn", RecordType.A)
    assert restored.value == original.value
    assert repository.sites.get_site(original.site_name).origin_host == original.value


def test_rollback_restoration_failure_is_atomic_and_never_reports_success(settings):
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner([ansible_run(host_run("edge-a")) for _ in range(2)]),
    )  # type: ignore[arg-type]
    original = _seed_proxied_record(control)
    successful = control.deployments.deploy("alice")
    control.dns.update_record(
        "example.com", "cdn", RecordType.A, RecordPatch(value="192.0.2.99"), "alice"
    )
    current = repository.zones.get_record("example.com", "cdn", RecordType.A)

    def fail_projection(_sites):
        raise RuntimeError("projection failed")

    repository.sites.replace_all_sites = fail_projection  # type: ignore[method-assign]
    result = control.deployments.rollback("alice", successful.id)

    assert result.status is DeploymentStatus.FAILED
    assert repository.zones.get_record("example.com", "cdn", RecordType.A) == current
    actions = [event.action for event in repository.audit_log.list_audit_events(10)]
    assert "rollback.applied" not in actions
    # Only the original deployment may have announced success.
    assert actions.count("deployment.succeeded") == 1
    assert original.value != current.value


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

    original_replace = repository.sites.replace_all_sites

    def recording_replace(sites):
        events.append("sites-replaced")
        original_replace(sites)

    repository.sites.replace_all_sites = recording_replace  # type: ignore[method-assign]
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=LockingRunner([ansible_run(host_run("edge-a")) for _ in range(2)]),
    )  # type: ignore[arg-type]
    original = CdnSite.model_validate(site_payload)
    repository.sites.create_site(original)
    successful = control.deployments.deploy("alice")
    repository.sites.replace_site(
        original.model_copy(update={"origin_host": "192.0.2.99"})
    )

    control.deployments.rollback("alice", successful.id)

    assert events == [
        "locked",
        "unlocked",  # the initial deploy
        "locked",
        "sites-replaced",
        "unlocked",
    ]


def test_a_stopped_fleet_deploy_names_the_edges_it_never_reached(
    settings, site_payload
):
    """`serial` plus `any_errors_fatal` leaves the rest of the fleet untouched.

    The play stops at the batch that failed, so later batches are never
    contacted: they do not fail, they simply never appear in the result, and a
    reader sees a smaller fleet rather than a split one. Half the edges are now
    on the new configuration and half on the old, which is the fact an operator
    most needs and the one `hosts` cannot carry.
    """
    repository = Repository(settings.database_path)
    stopped = ansible_run(
        host_run("edge-a", changed=3),
        host_run("edge-b", failed=1, failure="nginx -t refused it"),
        status=RunStatus.FAILED,
        return_code=2,
        targeted=("edge-a", "edge-b", "edge-c", "edge-d"),
    )
    control = ControlPlane(
        settings=settings, repository=repository, runner=FakeRunner([stopped])
    )  # type: ignore[arg-type]
    repository.sites.create_site(CdnSite.model_validate(site_payload))

    deployment = control.deployments.deploy("alice")

    assert deployment.status is DeploymentStatus.FAILED
    assert deployment.unattempted == ("edge-c", "edge-d")
    # And it reaches the operator, rather than only being available to ask for.
    assert "edge-c, edge-d" in (deployment.detail or "")
    assert "never attempted" in (deployment.detail or "")


def test_a_drift_check_that_stopped_early_is_not_in_sync(settings, site_payload):
    """An edge the check never got to is one we have no answer for."""
    repository = Repository(settings.database_path)
    partial = ansible_run(
        host_run("edge-a", changed=0),
        targeted=("edge-a", "edge-b"),
    )
    control = ControlPlane(
        settings=settings, repository=repository, runner=FakeRunner([partial])
    )  # type: ignore[arg-type]
    repository.sites.create_site(CdnSite.model_validate(site_payload))

    report = control.deployments.check_drift("alice")

    assert report.unattempted == ("edge-b",)
    assert report.in_sync is False


def test_origins_are_probed_by_the_edges_not_the_controller(settings, site_payload):
    """The check runs on the machines that carry the traffic.

    The controller's routes, resolver and egress rules are not the fleet's: an
    origin allow-listing the edges refuses the controller while working
    perfectly, and one reachable only from the controller's subnet passed the
    old check and then 502'd on every edge. What the controller still owns is
    *describing* the origin — port and SNI — so the two probes cannot disagree
    about what a site's origin is.
    """
    repository = Repository(settings.database_path)
    fake = FakeRunner([ansible_run(origin_report("edge-a"), origin_report("edge-b"))])
    control = ControlPlane(settings=settings, repository=repository, runner=fake)  # type: ignore[arg-type]
    repository.sites.create_site(CdnSite.model_validate(site_payload))

    report = control.edges.check_origins("alice", host_limit="edge-*")

    sent, limit = fake.origin_checks[0]
    assert limit == "edge-*"
    assert sent[0]["origin_port"] == 80
    assert sent[0]["ssl_mode"] == "off"
    assert "origin_scheme" not in sent[0]
    assert report.healthy is True
    assert [edge.host for edge in report.reporting] == ["edge-a", "edge-b"]


def test_an_origin_only_some_edges_can_reach_names_them(settings, site_payload):
    """The distinction a single vantage point could never have made."""
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner(
            [
                ansible_run(
                    origin_report("edge-a"),
                    origin_report("edge-b", reachable=False, detail="timed out"),
                )
            ]
        ),  # type: ignore[arg-type]
    )
    repository.sites.create_site(CdnSite.model_validate(site_payload))

    report = control.edges.check_origins("alice")

    assert report.healthy is False
    assert report.failing_sites == {"cdn-example-com": ("edge-b",)}
    assert report.silent == ()


def test_a_silent_edge_is_not_a_passing_edge(settings, site_payload):
    """An edge that said nothing has not confirmed anything."""
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner([ansible_run(origin_report("edge-a"), host_run("edge-b"))]),
    )  # type: ignore[arg-type]
    repository.sites.create_site(CdnSite.model_validate(site_payload))

    report = control.edges.check_origins("alice")

    assert [edge.host for edge in report.silent] == ["edge-b"]
    assert report.silent[0].error == "the edge published no report"


def test_startup_recovery_abandons_what_a_dead_process_left_behind(settings):
    """The case startup recovery exists for: nobody is deploying."""
    repository = Repository(settings.database_path)
    queue = RecordingBackgroundQueue()
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner(),  # type: ignore[arg-type]
        background=queue,
    )
    with repository.transaction():
        stranded = repository.deployments.create_deployment("alice", check_mode=False)
        repository.deployments.transition(
            stranded.id, DeploymentStatus.QUEUED, DeploymentStatus.RUNNING
        )
        workflow = repository.workflows.create(
            "interrupted", WorkflowKind.CERTIFICATE, "alice", "cdn-example-com"
        )
        repository.workflows.advance(workflow.id, WorkflowStatus.RUNNING)

    assert control.deployments.initialize() == 1
    assert (
        repository.deployments.get_deployment(stranded.id).status
        is DeploymentStatus.ABANDONED
    )
    assert repository.workflows.get(workflow.id).status is WorkflowStatus.NEEDS_REVIEW


def test_startup_recovery_leaves_a_live_deployment_alone(settings):
    """Restarting the API must not rewrite a run another process is doing.

    Status alone cannot distinguish "orphaned by a process that died" from
    "being converged right now", and the second is ordinary: an upgrade
    restarts the API while a CLI deploy is minutes into a run. Abandoning it
    would rewrite the record of a deployment still changing edges, and its own
    final transition would then fail against the status recovery had written.
    """
    repository = Repository(settings.database_path)

    class BusyRunner(FakeRunner):
        def lock(self):
            raise DeploymentBusyError("another deployment is already running")

    control = ControlPlane(
        settings=settings, repository=repository, runner=BusyRunner()
    )  # type: ignore[arg-type]
    live = repository.deployments.create_deployment("alice", check_mode=False)
    workflow = repository.workflows.create(
        "live", WorkflowKind.CERTIFICATE, "alice", "cdn-example-com"
    )
    repository.workflows.advance(workflow.id, WorkflowStatus.RUNNING)

    assert control.deployments.initialize() == 0
    assert (
        repository.deployments.get_deployment(live.id).status is DeploymentStatus.QUEUED
    )
    assert repository.workflows.get(workflow.id).status is WorkflowStatus.RUNNING


def test_busy_external_work_does_not_create_a_false_workflow(settings):
    """A workflow starts only after this process owns the external-work lock.

    Otherwise an API restart can see the journal entry, fail to acquire the
    lock held by the real worker, and report the refused attempt as interrupted
    work even though that attempt never touched an edge or a CA.
    """
    repository = Repository(settings.database_path)

    class BusyRunner(FakeRunner):
        def lock(self):
            raise DeploymentBusyError("another deployment is already running")

    control = ControlPlane(
        settings=settings, repository=repository, runner=BusyRunner()
    )  # type: ignore[arg-type]

    with pytest.raises(DeploymentBusyError):
        control.deployments.deploy("alice")
    with pytest.raises(DeploymentBusyError):
        control.certificates.request_certificate(
            "cdn-example-com", "alice", email="ops@example.com"
        )

    assert repository.workflows.list_workflows(10) == []


def test_a_renewal_blocked_by_a_deployment_is_skipped_not_failed(
    settings, certificate_pair, monkeypatch
):
    """Lock contention is "come back later", not "the CA refused".

    Issuance takes the deployment lock, so a fleet deploy can span a whole
    sweep. Filed under `failed` it read exactly like a CA rejection, which is
    the one thing a renewal report must not get wrong near an expiry — and the
    next run picks the site up regardless.
    """
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings, repository=repository, runner=FakeRunner()
    )  # type: ignore[arg-type]
    _seed_proxied_record(control)
    site = repository.sites.list_sites()[0]
    certificate, key = certificate_pair((site.server_names[0],), days=5)
    control._certificate_store.install(
        site, certificate, key, source=CertificateSource.ACME, email="ops@example.com"
    )

    def busy(*_args, **_kwargs):
        raise DeploymentBusyError("another deployment is already running")

    monkeypatch.setattr(control.certificates, "request_certificate", busy)

    result = control.certificates.renew_certificates("alice")

    assert result.failed == ()
    assert len(result.skipped) == 1
    assert "a deployment was running" in result.skipped[0]
    assert result.ok is True


def test_an_interrupted_issuance_says_how_far_it_got(settings, monkeypatch):
    """The CA may have issued a certificate that reached no disk here.

    That is a rate-limited issuance spent on nothing, and it is the state worth
    recognising before retrying — so the journal has to distinguish it from an
    interruption after the PEM was stored, which the next issuance corrects for
    free. Both used to arrive as one undifferentiated NEEDS_REVIEW.
    """
    configured = settings.model_copy(update={"acme_default_email": "ops@example.com"})
    repository = Repository(configured.database_path)
    control = ControlPlane(
        settings=configured,
        repository=repository,
        runner=FakeRunner(),
        preflight=FakePreflight(),
    )  # type: ignore[arg-type]
    _seed_proxied_record(control)

    class DyingStore:
        def install(self, *_args, **_kwargs):
            raise OSError("disk full")

    control.certificates.persistence = replace(
        control.certificates.persistence, certificates=DyingStore()
    )  # type: ignore[arg-type]
    monkeypatch.setattr(
        control.certificates.execution.issuer,
        "issue",
        lambda *_a, **_k: (b"cert", b"key"),
    )

    with pytest.raises(OSError):
        control.certificates.request_certificate("cdn-example-com", "alice")

    workflow = repository.workflows.list_workflows(10)[0]
    assert workflow.status is WorkflowStatus.FAILED
    # It got past the CA and no further, which is the whole distinction.
    assert [step.name for step in workflow.steps] == ["issued"]


def test_a_rollback_refuses_to_adopt_over_a_concurrent_record_write(settings):
    """The lost update rollback used to make silently.

    Record writes deliberately do not take the deployment lock, and adoption
    restores wholesale — so a record created during a minutes-long fleet
    rollback was deleted by the adoption that followed it, with no conflict and
    an audit trail showing it created and never removed. The write is done from
    inside the runner here because that is exactly when the window is open.
    """
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner([ansible_run(host_run("edge-a")) for _ in range(2)]),
    )  # type: ignore[arg-type]
    _seed_proxied_record(control)
    successful = control.deployments.deploy("alice")

    concurrent = DnsRecord(
        domain="example.com", name="late", value="198.51.100.77", proxied=True
    )

    class WritingRunner(FakeRunner):
        def run(self, *, check, host_limit=None):
            # Mid-run: the fleet is converging the old snapshot while an
            # operator adds a record the rollback has never heard of.
            control.dns.create_record(concurrent, "bob")
            return super().run(check=check, host_limit=host_limit)

    control._runner = WritingRunner([ansible_run(host_run("edge-a"))])
    control.deployments.execution = replace(
        control.deployments.execution, runner=control._runner
    )

    rolled_back = control.deployments.rollback("alice", successful.id)

    assert rolled_back.status is DeploymentStatus.FAILED
    assert "changed while this rollback was converging" in (rolled_back.detail or "")
    # The whole point: the record that arrived late is still there.
    assert (
        repository.zones.get_record("example.com", "late", RecordType.A) == concurrent
    )


def test_a_rollback_adopts_when_nothing_moved_under_it(settings):
    """The guard must not refuse the ordinary case."""
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner([ansible_run(host_run("edge-a")) for _ in range(3)]),
    )  # type: ignore[arg-type]
    original = _seed_proxied_record(control)
    successful = control.deployments.deploy("alice")
    control.dns.update_record(
        "example.com",
        "cdn",
        RecordType.A,
        RecordPatch(value="203.0.113.55"),
        "alice",
    )

    rolled_back = control.deployments.rollback("alice", successful.id)

    assert rolled_back.status is DeploymentStatus.SUCCEEDED
    assert repository.zones.get_record("example.com", "cdn", RecordType.A) == original
