import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml
from conftest import (
    FakeEdgeStore,
    FakePreflight,
    FakeRunner,
    InlineBackgroundRunner,
    RefusingBackgroundRunner,
    ansible_run,
    host_run,
    origin_report,
)

from blitzecdn.control_plane import ControlPlane
from blitzecdn.domain.cache import PurgeEntry
from blitzecdn.domain.certificates import CertificateSource
from blitzecdn.domain.deployments import DeploymentStatus
from blitzecdn.domain.dns import DnsRecord, Domain, RecordPatch, RecordType
from blitzecdn.domain.operations import WorkflowKind, WorkflowStatus
from blitzecdn.domain.runs import RunStatus
from blitzecdn.domain.sites import CdnSite, CertificateMode, HttpScheme
from blitzecdn.exceptions import (
    ConflictError,
    DeploymentBusyError,
    ExecutionError,
    NotFoundError,
)
from blitzecdn.infrastructure.database import Repository


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


def test_control_plane_closes_only_the_repository_it_owns(settings, monkeypatch):
    closed: list[Repository] = []
    monkeypatch.setattr(
        Repository, "close", lambda repository: closed.append(repository)
    )

    owned = ControlPlane(settings)
    owned.close()
    owned.close()

    injected_repository = Repository(settings.database_path)
    injected = ControlPlane(settings, injected_repository)
    injected.close()

    assert len(closed) == 1


def test_dns_write_projection_and_audit_are_one_transaction(settings):
    repository = Repository(settings.database_path)
    # Built with an integration so the outbox is wired: the property under test
    # is that an enqueued event is part of the same transaction as the write
    # that announced it, and there is nothing to enqueue without one.
    control = ControlPlane(
        settings,
        repository,
        FakeRunner(),  # type: ignore[arg-type]
        integrations={"domain.event": lambda _event: None},
    )
    control.dns.create_domain(Domain(name="example.com"), "alice")

    def refuse_event(_event):
        raise RuntimeError("audit subscriber failed")

    # Registered after the real audit observer: both its audit insert and the
    # record/site writes must still be rolled back when this subscriber fails.
    control.bus.subscribe(refuse_event)
    with pytest.raises(RuntimeError, match="audit subscriber failed"):
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
    assert [event.payload["action"] for event in repository.outbox.pending()] == [
        "domain.created"
    ]
    assert [event.action for event in repository.audit_log.list_audit_events()] == [
        "domain.created"
    ]


def test_no_integration_means_nothing_is_enqueued(settings):
    """The outbox is a delivery queue, so an unread one is only a leak.

    It used to be subscribed unconditionally against a handler that discarded
    what it was given, and dispatched only when an API process started — so a
    controller driven by the CLI and the timers wrote rows forever that nothing
    would ever read. The audit trail, which is subscribed either way, is what
    records that something happened.
    """
    repository = Repository(settings.database_path)
    control = ControlPlane(settings, repository, FakeRunner())  # type: ignore[arg-type]

    control.dns.create_domain(Domain(name="example.com"), "alice")

    assert control.outbox.enabled is False
    assert repository.outbox.pending() == []
    assert repository.outbox.undelivered() == 0
    assert [event.action for event in repository.audit_log.list_audit_events()] == [
        "domain.created"
    ]


def test_dispatch_delivers_then_bounds_the_receipts(settings):
    """Delivered rows are receipts; something has to drop the oldest.

    Pruned by the dispatcher rather than by a timer of its own, so retention
    cannot silently stop being applied because a unit was never installed.
    """
    delivered: list[str] = []
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings,
        repository,
        FakeRunner(),  # type: ignore[arg-type]
        integrations={"domain.event": lambda event: delivered.append(event.event_key)},
    )
    for index in range(4):
        control.dns.create_domain(Domain(name=f"example{index}.com"), "alice")

    assert repository.outbox.undelivered() == 4
    assert control.outbox.dispatch() == 4
    assert len(delivered) == 4
    assert repository.outbox.undelivered() == 0

    # Retention applies to what has been delivered, never to what has not.
    assert repository.outbox.prune_delivered(1) == 3
    assert control.outbox.dispatch() == 0


def test_projection_drift_is_detected_and_repairable(settings):
    repository = Repository(settings.database_path)
    control = ControlPlane(settings, repository, FakeRunner())  # type: ignore[arg-type]
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

    control = ControlPlane(settings, repository, TransactionAwareRunner())  # type: ignore[arg-type]
    control.deployments.deploy("alice")


def test_crud_validate_and_successful_deploy(settings):
    repository = Repository(settings.database_path)
    runner = FakeRunner([ansible_run(host_run("edge-a")) for _ in range(2)])
    control = ControlPlane(settings, repository, runner)  # type: ignore[arg-type]
    control.dns.create_domain(Domain(name="example.com"), "alice")
    record = control.dns.create_record(
        DnsRecord(
            domain="example.com", name="cdn", value="198.51.100.10", proxied=True
        ),
        "alice",
    )
    control.dns.update_record(
        "example.com", "cdn", RecordType.A, RecordPatch(cache_enabled=False), "alice"
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
    control = ControlPlane(settings, repository, FakeRunner())  # type: ignore[arg-type]

    assert control.deployments.deploy("alice").status is DeploymentStatus.SUCCEEDED
    desired = settings.generated_vars_path.read_text(encoding="utf-8")
    assert "blitzecdn_nginx_allow_empty_sites: false" in desired

    approved = ControlPlane(
        settings.model_copy(update={"allow_empty_sites": True}),
        repository,
        FakeRunner(),
    )  # type: ignore[arg-type]
    assert approved.deployments.deploy("alice").status is DeploymentStatus.SUCCEEDED
    desired = settings.generated_vars_path.read_text(encoding="utf-8")
    assert "blitzecdn_nginx_allow_empty_sites: true" in desired


def test_interrupted_deployment_is_recorded_as_abandoned(settings):
    class InterruptedRunner(FakeRunner):
        def run(self, *, check: bool, host_limit: str | None = None):
            raise KeyboardInterrupt

    repository = Repository(settings.database_path)
    control = ControlPlane(settings, repository, InterruptedRunner())  # type: ignore[arg-type]

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
    control = ControlPlane(settings, repository, FakeRunner())  # type: ignore[arg-type]
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
    control = ControlPlane(settings, repository, FakeRunner())  # type: ignore[arg-type]
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
    control = ControlPlane(settings, repository, FakeRunner())  # type: ignore[arg-type]
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
    control = ControlPlane(settings, repository, FakeRunner())  # type: ignore[arg-type]
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
    control = ControlPlane(settings, repository, FakeRunner())  # type: ignore[arg-type]
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
    control = ControlPlane(settings, repository, runner)  # type: ignore[arg-type]
    assert control.deployments.deploy("alice").status is DeploymentStatus.FAILED
    assert (
        control.deployments.deploy("alice", check=True).status
        is DeploymentStatus.TIMED_OUT
    )
    assert runner.check_modes == [False, True]


def test_rollback_updates_canonical_state_only_after_success(settings, site_payload):
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings,
        repository,
        FakeRunner([ansible_run(host_run("edge-a")) for _ in range(2)]),
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
        settings,
        repository,
        FakeRunner([ansible_run(host_run("edge-a")) for _ in range(2)]),
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
        settings,
        repository,
        LockingRunner([ansible_run(host_run("edge-a")) for _ in range(2)]),
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
    control = ControlPlane(settings, repository, FakeRunner([stopped]))  # type: ignore[arg-type]
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
    control = ControlPlane(settings, repository, FakeRunner([partial]))  # type: ignore[arg-type]
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
    control = ControlPlane(settings, repository, fake)  # type: ignore[arg-type]
    repository.sites.create_site(CdnSite.model_validate(site_payload))

    report = control.edges.check_origins("alice", host_limit="edge-*")

    sent, limit = fake.origin_checks[0]
    assert limit == "edge-*"
    assert sent[0]["origin_port"] == 443
    assert sent[0]["origin_scheme"] == "https"
    assert report.healthy is True
    assert [edge.host for edge in report.reporting] == ["edge-a", "edge-b"]


def test_an_origin_only_some_edges_can_reach_names_them(settings, site_payload):
    """The distinction a single vantage point could never have made."""
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings,
        repository,
        FakeRunner(
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
        settings,
        repository,
        FakeRunner([ansible_run(origin_report("edge-a"), host_run("edge-b"))]),
    )  # type: ignore[arg-type]
    repository.sites.create_site(CdnSite.model_validate(site_payload))

    report = control.edges.check_origins("alice")

    assert [edge.host for edge in report.silent] == ["edge-b"]
    assert report.silent[0].error == "the edge published no report"


def test_startup_recovery_abandons_what_a_dead_process_left_behind(settings):
    """The case startup recovery exists for: nobody is deploying."""
    repository = Repository(settings.database_path)
    control = ControlPlane(settings, repository, FakeRunner())  # type: ignore[arg-type]
    stranded = repository.deployments.create_deployment("alice", check_mode=False)

    assert control.deployments.initialize() == 1
    assert (
        repository.deployments.get_deployment(stranded.id).status
        is DeploymentStatus.ABANDONED
    )


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

    control = ControlPlane(settings, repository, BusyRunner())  # type: ignore[arg-type]
    live = repository.deployments.create_deployment("alice", check_mode=False)

    assert control.deployments.initialize() == 0
    assert (
        repository.deployments.get_deployment(live.id).status is DeploymentStatus.QUEUED
    )


def test_submit_deployment_queues_and_converges_on_a_worker(settings, site_payload):
    repository = Repository(settings.database_path)
    release = threading.Event()

    class BlockingRunner(FakeRunner):
        def run(self, *, check, host_limit=None):
            assert release.wait(timeout=5), "worker never reached the runner"
            return super().run(check=check)

    control = ControlPlane(settings, repository, BlockingRunner())  # type: ignore[arg-type]
    repository.sites.create_site(CdnSite.model_validate(site_payload))

    queued = control.deployments.submit_deployment("alice")
    assert queued.status is DeploymentStatus.QUEUED

    release.set()
    assert _await_terminal(repository, queued.id) is DeploymentStatus.SUCCEEDED


def test_a_queued_deployment_leaves_a_workflow_record(settings, site_payload):
    """The queued path is the one that most needs a durable trace.

    It answers before the convergence happens, so the run outlives the call
    that started it and a restart in between is exactly what the workflow
    journal exists to make legible. The synchronous path recorded one and this
    one did not, which had it backwards.
    """
    repository = Repository(settings.database_path)
    control = ControlPlane(settings, repository, FakeRunner())  # type: ignore[arg-type]
    repository.sites.create_site(CdnSite.model_validate(site_payload))

    queued = control.deployments.submit_deployment("alice")
    assert _await_terminal(repository, queued.id) is DeploymentStatus.SUCCEEDED

    assert _await_workflow(repository, queued.id) is WorkflowStatus.SUCCEEDED
    workflows = repository.workflows.list_workflows(10)
    assert [workflow.kind for workflow in workflows] == [WorkflowKind.DEPLOYMENT]
    assert workflows[0].resource_id == queued.id
    assert [step.name for step in workflows[0].steps] == ["converged"]


def test_a_failed_queued_deployment_fails_its_workflow(settings, site_payload):
    """A workflow that succeeded while its deployment failed would mislead."""
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings,
        repository,
        FakeRunner(
            [
                ansible_run(
                    host_run("edge-a", failed=1, failure="nginx -t refused it"),
                    status=RunStatus.FAILED,
                    return_code=2,
                )
            ]
        ),
    )  # type: ignore[arg-type]
    repository.sites.create_site(CdnSite.model_validate(site_payload))

    queued = control.deployments.submit_deployment("alice")
    assert _await_terminal(repository, queued.id) is DeploymentStatus.FAILED

    assert _await_workflow(repository, queued.id) is WorkflowStatus.FAILED
    assert repository.workflows.list_workflows(10)[0].error


def test_submit_rollback_reports_conflicts_synchronously(settings):
    """Nothing to roll back to must surface as an error, not a queued record."""
    repository = Repository(settings.database_path)
    control = ControlPlane(settings, repository, FakeRunner())  # type: ignore[arg-type]
    with pytest.raises(NotFoundError):
        control.deployments.submit_rollback("alice")


def test_submit_releases_the_lock_after_the_worker_finishes(settings, site_payload):
    repository = Repository(settings.database_path)
    control = ControlPlane(settings, repository, FakeRunner())  # type: ignore[arg-type]
    repository.sites.create_site(CdnSite.model_validate(site_payload))

    queued = control.deployments.submit_deployment("alice")
    assert _await_terminal(repository, queued.id) is DeploymentStatus.SUCCEEDED
    # A second submission proves the worker handed the lock back.
    control.runner.results = [ansible_run(host_run("edge-a"))]
    again = control.deployments.submit_deployment("alice")
    assert _await_terminal(repository, again.id) is DeploymentStatus.SUCCEEDED


def test_a_queued_deployment_converges_whatever_carries_the_work(
    settings, site_payload
):
    """The queued path, observed without waiting on a thread.

    ``submit_deployment`` answers before the run finishes, which used to mean a
    test of it either polled or raced. The runner that carries the work is a
    port, so supplying one that runs inline makes the whole queued path — lock,
    QUEUED record, converge, release — a synchronous assertion, and what is left
    untested is only the threading itself.
    """
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings,
        repository,
        FakeRunner(),  # type: ignore[arg-type]
        background=InlineBackgroundRunner(),
    )
    repository.sites.create_site(CdnSite.model_validate(site_payload))

    queued = control.deployments.submit_deployment("alice")

    # No _await_terminal: the work is already done by the time submit returns.
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

    Driven through the port rather than by monkeypatching ``Thread.start``:
    refusing to start is part of the ``BackgroundRunner`` contract, so a runner
    that refuses is a legitimate implementation of it rather than a global the
    test has to remember to undo.
    """
    repository = Repository(settings.database_path)
    refusing = RefusingBackgroundRunner()
    control = ControlPlane(
        settings,
        repository,
        FakeRunner(),  # type: ignore[arg-type]
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
    control.runner.results = [ansible_run(host_run("edge-a"))]
    again = control.deployments.submit_deployment("alice")
    assert _await_terminal(repository, again.id) is DeploymentStatus.SUCCEEDED


def test_runner_errors_are_recorded_and_reraised(settings, site_payload):
    class ExplodingRunner(FakeRunner):
        def run(self, *, check, host_limit=None):
            raise ExecutionError("unable to execute Ansible")

    repository = Repository(settings.database_path)
    control = ControlPlane(settings, repository, ExplodingRunner())  # type: ignore[arg-type]
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
    """An exception on the worker thread must not strand the deployment lock."""
    repository = Repository(settings.database_path)
    calls: list[int] = []

    class ExplodingOnceRunner(FakeRunner):
        def run(self, *, check, host_limit=None):
            calls.append(1)
            if len(calls) == 1:
                raise ExecutionError("boom")
            return ansible_run(host_run("edge-a"))

    control = ControlPlane(settings, repository, ExplodingOnceRunner())  # type: ignore[arg-type]
    repository.sites.create_site(CdnSite.model_validate(site_payload))

    first = control.deployments.submit_deployment("alice")
    assert _await_terminal(repository, first.id) is DeploymentStatus.FAILED

    second = control.deployments.submit_deployment("alice")
    assert _await_terminal(repository, second.id) is DeploymentStatus.SUCCEEDED


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
        preflight=FakePreflight(),
    )  # type: ignore[arg-type]
    _seed_proxied_record(control)
    certificate, key = certificate_pair()

    uploaded = control.certificates.upload_certificate(
        "cdn-example-com", certificate, key, "alice"
    )
    assert uploaded.source == "uploaded"
    assert repository.sites.get_site("cdn-example-com").certificate_mode == "uploaded"

    requested = control.certificates.request_certificate(
        "cdn-example-com", "alice", "owner@example.com"
    )
    assert requested.source == "acme"
    assert control.certificates.certificate("cdn-example-com") == requested
    assert repository.sites.get_site("cdn-example-com").certificate_mode == "requested"

    result = control.deployments.deploy("alice", check=True)
    assert result.status is DeploymentStatus.SUCCEEDED
    desired = settings.generated_vars_path.read_text(encoding="utf-8")
    assert "certificate_source_path" in desired
    assert "PRIVATE KEY" not in desired


def test_reconcile_issues_ready_first_certificate_and_deploys(
    settings, certificate_pair
):
    class FakeIssuer:
        def issue(self, site, email):
            assert email == "ops@example.com"
            return certificate_pair((site.server_names[0],))

    configured = settings.model_copy(update={"acme_default_email": "ops@example.com"})
    repository = Repository(configured.database_path)
    control = ControlPlane(
        configured,
        repository,
        FakeRunner([ansible_run(host_run("edge-a"))]),
        issuer=FakeIssuer(),
        preflight=FakePreflight(),
    )  # type: ignore[arg-type]
    site_name = _seed_proxied_record(control).site_name

    result = control.certificates.reconcile_certificates("timer")

    assert result["issued"] == [site_name]
    assert result["skipped"] == {}
    assert result["failed"] == {}
    assert result["deployment"].status is DeploymentStatus.SUCCEEDED
    assert repository.sites.get_site(site_name).certificate_mode == "requested"


def test_reconcile_skips_blocked_site_without_contacting_ca(settings, certificate_pair):
    class UnexpectedIssuer:
        def issue(self, _site, _email):
            raise AssertionError("blocked preflight must not contact the CA")

    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings,
        repository,
        FakeRunner(),
        issuer=UnexpectedIssuer(),
        preflight=FakePreflight(("dns",)),
    )  # type: ignore[arg-type]
    site_name = _seed_proxied_record(control).site_name

    result = control.certificates.reconcile_certificates("timer")

    assert result["issued"] == []
    assert "dns" in result["skipped"][site_name]
    assert result["deployment"] is None


def test_request_certificate_requires_email(settings, site_payload):
    repository = Repository(settings.database_path)
    repository.sites.create_site(CdnSite.model_validate(site_payload))
    control = ControlPlane(settings, repository, FakeRunner())  # type: ignore[arg-type]
    from blitzecdn.exceptions import ConflictError

    with pytest.raises(ConflictError, match="email"):
        control.certificates.request_certificate("cdn-example-com", "alice")


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

    control.certificates.upload_certificate(
        "cdn-example-com", certificate, key, "alice"
    )

    assert events == ["locked", "installed", "unlocked"]


def test_a_canary_records_its_limit_and_passes_it_to_ansible(settings, site_payload):
    repository = Repository(settings.database_path)
    runner = FakeRunner([ansible_run(host_run("edge-a"))])
    control = ControlPlane(settings, repository, runner)  # type: ignore[arg-type]
    repository.sites.create_site(CdnSite.model_validate(site_payload))

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
    control = ControlPlane(settings, repository, runner)  # type: ignore[arg-type]

    # Three distinct desired states, made the only supported way: by changing
    # records. A snapshot carries records and derives sites from them, so
    # editing the derived table would produce three identical snapshots.
    repository.zones.create_domain(Domain(name="example.com"))
    repository.zones.create_record(
        DnsRecord(domain="example.com", name="cdn", value="198.51.100.10", proxied=True)
    )
    full = control.deployments.deploy("alice")

    repository.zones.delete_record("example.com", "cdn", RecordType.A)
    canary = control.deployments.deploy("alice", host_limit="edge-a")
    assert canary.status is DeploymentStatus.SUCCEEDED

    # A third, distinct state, so both earlier snapshots are eligible and the
    # canary is the more recent of the two. Without the filter it would win.
    repository.zones.create_record(
        DnsRecord(
            domain="example.com", name="other", value="198.51.100.11", proxied=True
        )
    )
    assert (
        repository.deployments.successful_rollback_target(repository.snapshot()).id
        == full.id
    )


def test_a_malformed_limit_is_refused_before_a_deployment_is_recorded(
    settings, site_payload
):
    repository = Repository(settings.database_path)
    control = ControlPlane(settings, repository, FakeRunner())  # type: ignore[arg-type]
    repository.sites.create_site(CdnSite.model_validate(site_payload))

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


def test_drift_check_runs_without_changing_anything(settings, site_payload):
    repository = Repository(settings.database_path)
    runner = FakeRunner([_in_sync_run()])
    control = ControlPlane(settings, repository, runner)  # type: ignore[arg-type]
    repository.sites.create_site(CdnSite.model_validate(site_payload))

    report = control.deployments.check_drift("alice")

    assert runner.check_modes == [True], "a drift check must never apply changes"
    assert report.in_sync is True
    assert report.drifted == ()


def test_drift_check_names_the_edges_that_moved(settings, site_payload):
    repository = Repository(settings.database_path)
    runner = FakeRunner([_drifted_run()])
    control = ControlPlane(settings, repository, runner)  # type: ignore[arg-type]
    repository.sites.create_site(CdnSite.model_validate(site_payload))

    report = control.deployments.check_drift("alice")

    assert report.in_sync is False
    assert [host.host for host in report.drifted] == ["edge-a"]
    assert any(
        event.action == "drift.checked" and event.details["drifted"] == ["edge-a"]
        for event in repository.audit_log.list_audit_events(10)
    )


def test_a_drift_report_can_be_reread_from_the_recorded_deployment(
    settings, site_payload
):
    repository = Repository(settings.database_path)
    runner = FakeRunner([_drifted_run()])
    control = ControlPlane(settings, repository, runner)  # type: ignore[arg-type]
    repository.sites.create_site(CdnSite.model_validate(site_payload))

    first = control.deployments.check_drift("alice")
    again = control.deployments.drift_report(first.deployment_id)

    assert again.hosts == first.hosts


def test_an_applied_deployment_is_not_a_drift_report(settings, site_payload):
    """Its output says what it did, not what had drifted."""
    repository = Repository(settings.database_path)
    runner = FakeRunner([_drifted_run()])
    control = ControlPlane(settings, repository, runner)  # type: ignore[arg-type]
    repository.sites.create_site(CdnSite.model_validate(site_payload))

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
    record = _seed_proxied_record(control)
    certificate, key = certificate_pair((record.fqdn,), days=days)
    return control.certificates.upload_certificate(
        record.site_name, certificate, key, "alice"
    )


def test_certificate_statuses_report_time_left(settings, certificate_pair):
    repository = Repository(settings.database_path)
    control = ControlPlane(settings, repository, FakeRunner())  # type: ignore[arg-type]
    _proxied_site_with_certificate(control, repository, certificate_pair, days=10)

    statuses = control.certificates.certificate_statuses()

    assert len(statuses) == 1
    assert statuses[0].days_remaining == 9  # a whole day has not yet elapsed
    assert statuses[0].renewable is False, "an uploaded certificate is not renewable"
    assert control.certificates.expiring_certificates() == statuses


def test_a_healthy_certificate_is_not_reported_as_expiring(settings, certificate_pair):
    repository = Repository(settings.database_path)
    control = ControlPlane(settings, repository, FakeRunner())  # type: ignore[arg-type]
    _proxied_site_with_certificate(control, repository, certificate_pair, days=89)

    assert control.certificates.certificate_statuses() != []
    assert control.certificates.expiring_certificates() == []


def test_renewal_reissues_only_what_is_due(settings, certificate_pair):
    repository = Repository(settings.database_path)
    issuer = _RecordingIssuer(certificate_pair)
    control = ControlPlane(
        settings,
        repository,
        FakeRunner(),  # type: ignore[arg-type]
        issuer=issuer,
        preflight=FakePreflight(),  # type: ignore[arg-type]
    )
    control.settings = settings

    control.dns.create_domain(Domain(name="example.com"), "alice")
    for label, days in (("due", 5), ("healthy", 80)):
        record = control.dns.create_record(
            DnsRecord(
                domain="example.com",
                name=label,
                value="198.51.100.10",
                proxied=True,
            ),
            "alice",
        )
        certificate, key = certificate_pair((record.fqdn,), days=days)
        control.certificates.upload_certificate(
            record.site_name, certificate, key, "alice"
        )
        # Uploaded certificates are never renewable, so re-request each one to
        # put it under ACME management the way a real ACME site would be.
        control.certificates.request_certificate(
            record.site_name, "alice", "ops@example.com"
        )
    issuer.issued.clear()

    # Both now carry the issuer's 90-day certificate, so nothing is due.
    assert control.certificates.renew_certificates("alice")["renewed"] == []
    assert issuer.issued == []

    assert sorted(
        control.certificates.renew_certificates("alice", force=True)["renewed"]
    ) == [
        "due-example-com",
        "healthy-example-com",
    ]


def test_a_spent_renewal_budget_stops_between_sites_and_says_so(
    settings, certificate_pair, monkeypatch
):
    """A truncated sweep is a slower renewal, not a missed one.

    The budget is checked between sites and never during one, so a request that
    runs out of time cannot leave the CA believing it issued a certificate this
    store never recorded. Whatever it did not reach comes back under `skipped`
    and is picked up by the next run.
    """
    repository = Repository(settings.database_path)
    issuer = _RecordingIssuer(certificate_pair)
    control = ControlPlane(
        settings,
        repository,
        FakeRunner(),  # type: ignore[arg-type]
        issuer=issuer,
        preflight=FakePreflight(),  # type: ignore[arg-type]
    )
    control.dns.create_domain(Domain(name="example.com"), "alice")
    for label in ("one", "two"):
        record = control.dns.create_record(
            DnsRecord(
                domain="example.com", name=label, value="198.51.100.10", proxied=True
            ),
            "alice",
        )
        certificate, key = certificate_pair((record.fqdn,), days=5)
        control.certificates.upload_certificate(
            record.site_name, certificate, key, "alice"
        )
        control.certificates.request_certificate(
            record.site_name, "alice", "ops@example.com"
        )
    issuer.issued.clear()

    # Time runs out the moment the first site has been renewed.
    clock = iter([0.0, 0.0, 1000.0, 1000.0, 1000.0])
    monkeypatch.setattr(
        "blitzecdn.application.certificates.monotonic", lambda: next(clock)
    )

    result = control.certificates.renew_certificates(
        "alice", force=True, budget_seconds=1
    )

    assert len(result["renewed"]) == 1
    assert len(issuer.issued) == 1, "the budget must not interrupt an issuance"
    assert len(result["skipped"]) == 1
    assert "renewal budget" in result["skipped"][0]
    assert "retried by the next run" in result["skipped"][0]


def test_renewal_without_a_budget_is_unbounded(settings, certificate_pair):
    """The CLI and the timer pass no budget, and must keep sweeping everything."""
    repository = Repository(settings.database_path)
    issuer = _RecordingIssuer(certificate_pair)
    control = ControlPlane(
        settings,
        repository,
        FakeRunner(),  # type: ignore[arg-type]
        issuer=issuer,
        preflight=FakePreflight(),  # type: ignore[arg-type]
    )
    control.dns.create_domain(Domain(name="example.com"), "alice")
    for label in ("one", "two"):
        record = control.dns.create_record(
            DnsRecord(
                domain="example.com", name=label, value="198.51.100.10", proxied=True
            ),
            "alice",
        )
        certificate, key = certificate_pair((record.fqdn,), days=5)
        control.certificates.upload_certificate(
            record.site_name, certificate, key, "alice"
        )
        control.certificates.request_certificate(
            record.site_name, "alice", "ops@example.com"
        )

    result = control.certificates.renew_certificates("alice", force=True)

    assert len(result["renewed"]) == 2
    assert result["skipped"] == []


def test_an_uploaded_certificate_near_expiry_is_reported_not_renewed(
    settings, certificate_pair
):
    repository = Repository(settings.database_path)
    issuer = _RecordingIssuer(certificate_pair)
    control = ControlPlane(
        settings,
        repository,
        FakeRunner(),  # type: ignore[arg-type]
        issuer=issuer,
        preflight=FakePreflight(),  # type: ignore[arg-type]
    )
    _proxied_site_with_certificate(control, repository, certificate_pair, days=3)

    result = control.certificates.renew_certificates("alice")

    assert result["renewed"] == []
    assert issuer.issued == [], "BlitzeCDN must not reissue someone else's certificate"
    assert "uploaded, not issued by BlitzeCDN" in result["skipped"][0]


def test_one_failing_renewal_does_not_stop_the_others(settings, certificate_pair):
    """A scheduled renewal must make progress even when a site is unreachable."""
    repository = Repository(settings.database_path)
    issuer = _RecordingIssuer(certificate_pair, fails={"broken-example-com"})
    control = ControlPlane(
        settings,
        repository,
        FakeRunner(),  # type: ignore[arg-type]
        issuer=issuer,
        preflight=FakePreflight(),  # type: ignore[arg-type]
    )

    control.dns.create_domain(Domain(name="example.com"), "alice")
    for label in ("broken", "fine"):
        record = control.dns.create_record(
            DnsRecord(
                domain="example.com", name=label, value="198.51.100.10", proxied=True
            ),
            "alice",
        )
        certificate, key = certificate_pair((record.fqdn,), days=5)
        control.certificates.upload_certificate(
            record.site_name, certificate, key, "alice"
        )
        # Restamp the stored metadata as an ACME issue registered to an
        # address, which is what a real renewable certificate looks like.
        info = control.certificate_store.get(record.site_name)
        path = settings.certificate_dir / record.site_name / "metadata.json"
        path.write_text(
            info.model_copy(
                update={"source": CertificateSource.ACME, "email": "ops@example.com"}
            ).model_dump_json(indent=2),
            encoding="utf-8",
        )

    result = control.certificates.renew_certificates("alice")

    assert result["renewed"] == ["fine-example-com"]
    assert len(result["failed"]) == 1
    assert "broken-example-com" in result["failed"][0]


def _two_acme_sites(control, certificate_pair):
    """Two sites under ACME management, both freshly issued and not yet due."""
    control.dns.create_domain(Domain(name="example.com"), "alice")
    for label in ("first", "second"):
        record = control.dns.create_record(
            DnsRecord(
                domain="example.com", name=label, value="198.51.100.10", proxied=True
            ),
            "alice",
        )
        certificate, key = certificate_pair((record.fqdn,), days=80)
        control.certificates.upload_certificate(
            record.site_name, certificate, key, "alice"
        )
        control.certificates.request_certificate(
            record.site_name, "alice", "ops@example.com"
        )


def test_renewal_can_be_narrowed_to_named_sites(settings, certificate_pair):
    """Retrying one failure must not push the others through a rate-limited CA."""
    repository = Repository(settings.database_path)
    issuer = _RecordingIssuer(certificate_pair)
    control = ControlPlane(
        settings,
        repository,
        FakeRunner(),  # type: ignore[arg-type]
        issuer=issuer,
        preflight=FakePreflight(),  # type: ignore[arg-type]
    )
    _two_acme_sites(control, certificate_pair)
    issuer.issued.clear()

    result = control.certificates.renew_certificates(
        "alice", force=True, sites=["first-example-com"]
    )

    assert result["renewed"] == ["first-example-com"]
    # The unselected site never reached the CA at all.
    assert [site for site, _ in issuer.issued] == ["first-example-com"]


def test_renewal_rejects_a_site_it_has_no_certificate_for(settings, certificate_pair):
    """A typo must not read as 'nothing was due', which is how expiries are missed."""
    repository = Repository(settings.database_path)
    issuer = _RecordingIssuer(certificate_pair)
    control = ControlPlane(
        settings,
        repository,
        FakeRunner(),  # type: ignore[arg-type]
        issuer=issuer,
        preflight=FakePreflight(),  # type: ignore[arg-type]
    )
    _two_acme_sites(control, certificate_pair)
    issuer.issued.clear()

    with pytest.raises(NotFoundError, match="frist-example-com"):
        control.certificates.renew_certificates(
            "alice", force=True, sites=["frist-example-com"]
        )

    # Nothing was renewed before the unknown name was noticed.
    assert issuer.issued == []


def test_renewal_records_the_selector_in_the_audit_trail(settings, certificate_pair):
    repository = Repository(settings.database_path)
    issuer = _RecordingIssuer(certificate_pair)
    control = ControlPlane(
        settings,
        repository,
        FakeRunner(),  # type: ignore[arg-type]
        issuer=issuer,
        preflight=FakePreflight(),  # type: ignore[arg-type]
    )
    _two_acme_sites(control, certificate_pair)

    control.certificates.renew_certificates(
        "alice", force=True, sites=["second-example-com"]
    )
    narrowed = repository.audit_log.list_audit_events()[0]
    control.certificates.renew_certificates("alice")
    full = repository.audit_log.list_audit_events()[0]

    assert narrowed.action == "certificates.renewed"
    assert narrowed.details["sites"] == ["second-example-com"]
    # A full sweep is distinguishable from a narrowed one that renewed nothing.
    assert full.details["sites"] is None


# ----------------------------------------------------------------------
# Cache purge
# ----------------------------------------------------------------------


def _purge_run():
    return ansible_run(host_run("edge-a", changed=1))


def _site(control, repository, name="cdn-example-com", server="cdn.example.com"):
    repository.sites.create_site(
        CdnSite.model_validate(
            {"name": name, "server_names": [server], "origin_host": "o.example.com"}
        )
    )


def test_a_purge_reaches_the_edges_with_the_entries_it_was_given(settings):
    repository = Repository(settings.database_path)
    fake = FakeRunner([_purge_run()])
    control = ControlPlane(settings, repository, fake)  # type: ignore[arg-type]
    _site(control, repository)

    result = control.cache.purge_cache(
        "alice",
        entries=[
            PurgeEntry(host="cdn.example.com", uri="/app.js", scheme=HttpScheme.HTTP)
        ],
    )

    assert result.complete is True
    entries, purge_all, _ = fake.purges[0]
    assert entries == [{"host": "cdn.example.com", "uri": "/app.js", "scheme": "http"}]
    assert purge_all is False


def test_a_purge_for_a_hostname_no_site_serves_is_refused(settings):
    """Otherwise it reports success having removed nothing."""
    repository = Repository(settings.database_path)
    fake = FakeRunner([_purge_run()])
    control = ControlPlane(settings, repository, fake)  # type: ignore[arg-type]
    _site(control, repository)

    with pytest.raises(NotFoundError, match=re.escape("other.example.com")):
        control.cache.purge_cache(
            "alice", entries=[PurgeEntry(host="other.example.com", uri="/x")]
        )
    assert fake.purges == []


def test_a_purge_under_a_wildcard_site_is_allowed(settings):
    """nginx matches *.example.com to a.example.com, so purge must too."""
    repository = Repository(settings.database_path)
    fake = FakeRunner([_purge_run()])
    control = ControlPlane(settings, repository, fake)  # type: ignore[arg-type]
    _site(control, repository, server="*.assets.example.com")

    result = control.cache.purge_cache(
        "alice",
        entries=[
            PurgeEntry(
                host="img.assets.example.com", uri="/a.png", scheme=HttpScheme.HTTP
            )
        ],
    )
    assert result.complete is True


def test_a_purge_for_a_scheme_the_site_never_serves_is_refused(settings):
    """The cache key starts with $scheme, so the two are different entries.

    A site without TLS is served entirely over port 80, so nothing of it is
    stored under `https`. Purging that scheme computes a different MD5 and
    deletes a file that was never written, while every edge reports success.
    """
    repository = Repository(settings.database_path)
    fake = FakeRunner([_purge_run()])
    control = ControlPlane(settings, repository, fake)  # type: ignore[arg-type]
    _site(control, repository)

    with pytest.raises(ConflictError, match="scheme"):
        control.cache.purge_cache(
            "alice",
            entries=[
                PurgeEntry(
                    host="cdn.example.com", uri="/app.js", scheme=HttpScheme.HTTPS
                )
            ],
        )
    assert fake.purges == []


def test_a_purge_over_http_against_a_tls_site_is_refused(settings):
    """Port 80 only answers 308 for a TLS site, so it caches nothing."""
    repository = Repository(settings.database_path)
    fake = FakeRunner([_purge_run()])
    control = ControlPlane(settings, repository, fake)  # type: ignore[arg-type]
    repository.sites.create_site(
        CdnSite.model_validate(
            {
                "name": "tls-example-com",
                "server_names": ["tls.example.com"],
                "origin_host": "o.example.com",
                "certificate_mode": "existing",
                "certificate_path": "/etc/ssl/certs/tls.pem",
                "certificate_key_path": "/etc/ssl/private/tls.key",
            }
        )
    )

    with pytest.raises(ConflictError, match="scheme"):
        control.cache.purge_cache(
            "alice",
            entries=[
                PurgeEntry(
                    host="tls.example.com", uri="/app.js", scheme=HttpScheme.HTTP
                )
            ],
        )
    assert fake.purges == []


def test_a_purge_for_a_disabled_site_is_refused(settings):
    repository = Repository(settings.database_path)
    fake = FakeRunner([_purge_run()])
    control = ControlPlane(settings, repository, fake)  # type: ignore[arg-type]
    repository.sites.create_site(
        CdnSite.model_validate(
            {
                "name": "off-example-com",
                "server_names": ["off.example.com"],
                "origin_host": "o.example.com",
                "enabled": False,
            }
        )
    )

    with pytest.raises(NotFoundError):
        control.cache.purge_cache(
            "alice", entries=[PurgeEntry(host="off.example.com", uri="/x")]
        )


def test_purging_everything_and_named_entries_at_once_is_refused(settings):
    repository = Repository(settings.database_path)
    control = ControlPlane(settings, repository, FakeRunner())  # type: ignore[arg-type]
    _site(control, repository)

    with pytest.raises(ConflictError):
        control.cache.purge_cache(
            "alice",
            entries=[
                PurgeEntry(host="cdn.example.com", uri="/x", scheme=HttpScheme.HTTP)
            ],
            purge_all=True,
        )


def test_a_purge_with_nothing_to_do_is_refused(settings):
    control = ControlPlane(settings, Repository(settings.database_path), FakeRunner())  # type: ignore[arg-type]
    with pytest.raises(ConflictError):
        control.cache.purge_cache("alice")


def test_purging_everything_needs_no_site_to_exist(settings):
    """--all is about the cache on disk, not about what is currently declared."""
    fake = FakeRunner([_purge_run()])
    control = ControlPlane(settings, Repository(settings.database_path), fake)  # type: ignore[arg-type]

    result = control.cache.purge_cache("alice", purge_all=True)

    assert result.complete is True
    assert fake.purges[0][1] is True


def test_a_partial_purge_is_reported_as_incomplete(settings):
    """Some edges dropped the object and some did not: clients see both."""
    repository = Repository(settings.database_path)
    partial = ansible_run(
        host_run("edge-a", changed=1), host_run("edge-b", ok=0, unreachable=1)
    )
    control = ControlPlane(settings, repository, FakeRunner([partial]))  # type: ignore[arg-type]
    _site(control, repository)

    result = control.cache.purge_cache(
        "alice",
        entries=[
            PurgeEntry(host="cdn.example.com", uri="/app.js", scheme=HttpScheme.HTTP)
        ],
    )

    assert result.complete is False
    assert [host.host for host in result.failed] == ["edge-b"]


def test_a_purge_no_edge_answered_is_an_error(settings):
    """Silence is not success: the object may still be served everywhere."""
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings,
        repository,
        FakeRunner([ansible_run(status=RunStatus.FAILED, return_code=1)]),
    )  # type: ignore[arg-type]
    _site(control, repository)

    with pytest.raises(ExecutionError, match="no edge reported"):
        control.cache.purge_cache(
            "alice",
            entries=[
                PurgeEntry(host="cdn.example.com", uri="/x", scheme=HttpScheme.HTTP)
            ],
        )


def test_a_purge_is_recorded_in_the_audit_trail(settings):
    repository = Repository(settings.database_path)
    control = ControlPlane(settings, repository, FakeRunner([_purge_run()]))  # type: ignore[arg-type]
    _site(control, repository)

    control.cache.purge_cache(
        "alice",
        entries=[
            PurgeEntry(host="cdn.example.com", uri="/app.js", scheme=HttpScheme.HTTP)
        ],
    )

    event = repository.audit_log.list_audit_events()[0]
    assert event.action == "cache.purged"
    assert event.details["complete"] is True
    assert event.details["entries"][0]["uri"] == "/app.js"


# ----------------------------------------------------------------------
# Cache statistics
# ----------------------------------------------------------------------


def _stats_control(settings, *hosts):
    """A control plane whose next stats run returns these per-host results.

    Counters now arrive on the run itself, published by the role as the
    `blitzecdn_report` fact. There is no controller-side directory in the
    picture, so the roster and the numbers cannot disagree.
    """
    fake = FakeRunner([ansible_run(*hosts)])
    return ControlPlane(settings, Repository(settings.database_path), fake), fake  # type: ignore[arg-type]


def _report(cache, *, reachable=True):
    return {
        "host": "ignored",
        "collected_at": "2026-08-09T01:00:00Z",
        "nginx_reachable": reachable,
        "connections": {"active": 5, "requests": 100},
        "cache": cache,
    }


def _reporting(name, cache):
    return host_run(name, ok=5, report=_report(cache))


def test_statistics_are_aggregated_across_the_fleet(settings):
    control, _ = _stats_control(
        settings,
        _reporting(
            "edge-a",
            [
                {"site": "cdn.example.com", "outcome": "HIT", "requests": 7},
                {"site": "cdn.example.com", "outcome": "MISS", "requests": 3},
            ],
        ),
        _reporting(
            "edge-b", [{"site": "cdn.example.com", "outcome": "HIT", "requests": 10}]
        ),
    )

    report = control.cache.cache_stats("alice")

    assert report.hit_ratio == 0.85
    assert {edge.host for edge in report.reporting} == {"edge-a", "edge-b"}
    assert report.by_site()[0].site == "cdn.example.com"


def test_an_edge_that_published_nothing_is_silent_rather_than_missing(settings):
    """The run is the roster; a vanished edge would understate the fleet.

    An edge whose tasks ran but which published no report is a real state — the
    role failed part way, or a future role simply has nothing to say — and it
    has to read as silence rather than as absence.
    """
    control, _ = _stats_control(
        settings,
        _reporting("edge-a", [{"site": "a", "outcome": "HIT", "requests": 1}]),
        host_run("edge-b", ok=5),
    )

    report = control.cache.cache_stats("alice")

    assert [edge.host for edge in report.silent] == ["edge-b"]
    assert [edge.host for edge in report.reporting] == ["edge-a"]


def test_an_unreachable_edge_is_reported_as_unreachable(settings):
    control, _ = _stats_control(
        settings,
        _reporting("edge-a", [{"site": "a", "outcome": "HIT", "requests": 1}]),
        host_run("edge-b", ok=0, unreachable=1),
    )

    report = control.cache.cache_stats("alice")

    assert [(e.host, e.error) for e in report.silent] == [("edge-b", "unreachable")]


def test_an_edge_whose_stats_role_failed_is_not_counted(settings):
    """Its counters would be partial, and a partial number reads as a real one."""
    control, _ = _stats_control(
        settings,
        _reporting("edge-a", [{"site": "a", "outcome": "HIT", "requests": 1}]),
        host_run("edge-b", failure="collect-cache-stats.sh returned 1"),
    )

    report = control.cache.cache_stats("alice")

    assert [edge.host for edge in report.silent] == ["edge-b"]
    assert report.requests == 1


def test_a_malformed_edge_report_degrades_instead_of_raising(settings):
    """One bad payload must not take the whole fleet's numbers with it.

    The shape is ours, but it crossed a machine boundary, so every field is
    read defensively: rows that are not objects, counts that are not numbers
    and a timestamp that will not parse are dropped rather than raised on.
    """
    control, _ = _stats_control(
        settings,
        _reporting("edge-a", [{"site": "a", "outcome": "HIT", "requests": 1}]),
        host_run(
            "edge-b",
            ok=5,
            report={
                "collected_at": "not a timestamp",
                "connections": "not a mapping",
                "cache": ["not an object", {"site": "b", "requests": "many"}],
            },
        ),
    )

    report = control.cache.cache_stats("alice")

    assert report.requests == 1
    edge_b = next(edge for edge in report.edges if edge.host == "edge-b")
    assert edge_b.error is None
    assert edge_b.sites == ()
    assert edge_b.collected_at is None
    assert edge_b.connections == {}


def test_statistics_are_recorded_in_the_audit_trail(settings):
    control, _ = _stats_control(
        settings,
        _reporting("edge-a", [{"site": "a", "outcome": "HIT", "requests": 1}]),
        _reporting("edge-b", [{"site": "a", "outcome": "MISS", "requests": 1}]),
    )

    control.cache.cache_stats("alice")
    event = Repository(settings.database_path).audit_log.list_audit_events()[0]

    assert event.action == "cache.stats_collected"
    assert event.details["hit_ratio"] == 0.5


# ----------------------------------------------------------------------
# Certificate preflight enforcement
#
# The checks themselves are tested in test_preflight.py. These are about what
# the application does with a report: refuse, override, or pass through.
# ----------------------------------------------------------------------


def _preflight_control(settings, certificate_pair, failures=()):
    repository = Repository(settings.database_path)
    issuer = _RecordingIssuer(certificate_pair)
    preflight = FakePreflight(failures)
    control = ControlPlane(
        settings,
        repository,
        FakeRunner(),  # type: ignore[arg-type]
        issuer=issuer,
        preflight=preflight,  # type: ignore[arg-type]
    )
    return control, repository, issuer, preflight


def test_a_blocked_preflight_refuses_before_reaching_the_ca(settings, certificate_pair):
    """The rate limit is the thing being protected: no CA request at all."""
    control, _, issuer, _ = _preflight_control(settings, certificate_pair, ("dns",))
    _seed_proxied_record(control)

    with pytest.raises(ConflictError, match="preflight failed"):
        control.certificates.request_certificate(
            "cdn-example-com", "alice", "ops@example.com"
        )

    assert issuer.issued == []


def test_the_refusal_names_the_failed_check_and_the_way_past_it(
    settings, certificate_pair
):
    control, _, _, _ = _preflight_control(settings, certificate_pair, ("caa",))
    _seed_proxied_record(control)

    with pytest.raises(ConflictError) as raised:
        control.certificates.request_certificate(
            "cdn-example-com", "alice", "ops@example.com"
        )

    assert "caa" in str(raised.value)
    assert "skip_preflight" in str(raised.value)


def test_an_override_issues_and_is_audited_as_its_own_event(settings, certificate_pair):
    control, repository, issuer, _ = _preflight_control(
        settings, certificate_pair, ("dns", "deployed")
    )
    _seed_proxied_record(control)

    info = control.certificates.request_certificate(
        "cdn-example-com", "alice", "ops@example.com", skip_preflight=True
    )

    assert info.source == "acme"
    assert issuer.issued == [("cdn-example-com", "ops@example.com")]
    overrides = [
        event
        for event in repository.audit_log.list_audit_events()
        if event.action == "certificate.requested.preflight_overridden"
    ]
    assert len(overrides) == 1
    assert {failure["check"] for failure in overrides[0].details["failures"]} == {
        "dns",
        "deployed",
    }


def test_preflight_is_told_the_records_ttl(settings, certificate_pair):
    """The TTL advisory is only possible if the record's own value reaches it."""
    control, _, _, preflight = _preflight_control(settings, certificate_pair)
    control.dns.create_domain(Domain(name="example.com"), "alice")
    control.dns.create_record(
        DnsRecord(
            domain="example.com",
            name="cdn",
            value="198.51.100.10",
            proxied=True,
            ttl=7200,
        ),
        "alice",
    )

    control.certificates.request_certificate(
        "cdn-example-com", "alice", "ops@example.com"
    )

    assert preflight.calls[-1] == ("cdn-example-com", False, 7200)


def test_a_blocked_renewal_is_reported_as_failed_not_silently_skipped(
    settings, certificate_pair
):
    """A renewal that cannot validate has to reach the timer's exit code."""
    control, _, issuer, preflight = _preflight_control(settings, certificate_pair)
    _proxied_site_with_certificate(control, None, certificate_pair, days=3)
    control.certificates.request_certificate(
        "cdn-example-com", "alice", "ops@example.com"
    )
    issuer.issued.clear()
    preflight.failures = ("dns",)

    result = control.certificates.renew_certificates("alice", force=True)

    assert result["renewed"] == []
    assert len(result["failed"]) == 1
    assert "preflight failed" in result["failed"][0]
    assert issuer.issued == []


def test_a_check_mode_run_does_not_count_as_deployed(settings, certificate_pair):
    """Check mode proved the play parses; no edge is serving the vhost."""
    control, _, _, _ = _preflight_control(settings, certificate_pair)
    _seed_proxied_record(control)

    # Two runs, so the fake runner needs a result for each.
    control.runner.results.append(ansible_run(host_run("edge-a")))

    control.deployments.deploy("alice", check=True)
    assert control.deployments.site_is_deployed("cdn-example-com") is False

    control.deployments.deploy("alice")
    assert control.deployments.site_is_deployed("cdn-example-com") is True


def test_a_site_absent_from_the_last_deployment_is_not_deployed(
    settings, certificate_pair
):
    control, _, _, _ = _preflight_control(settings, certificate_pair)
    _seed_proxied_record(control)
    control.deployments.deploy("alice")

    assert control.deployments.site_is_deployed("cdn-example-com") is True
    assert control.deployments.site_is_deployed("other-example-com") is False


def test_certificate_preflight_reports_without_contacting_a_ca(
    settings, certificate_pair
):
    control, _, issuer, _ = _preflight_control(settings, certificate_pair, ("dns",))
    _seed_proxied_record(control)

    report = control.certificates.certificate_preflight("cdn-example-com")

    assert report.site == "cdn-example-com"
    assert not report.ok
    assert issuer.issued == []


def test_validate_never_writes_the_file_a_deploy_is_converging(settings):
    """Validation takes no lock, so it must not touch shared desired state.

    A deploy in another process owns `generated_vars_path` between writing its
    snapshot there and Ansible reading it. A validate landing in that window
    would converge the fleet to a document the deployment never recorded —
    worst of all under rollback, which would then rewrite canonical records to
    the old zones while the edges received current state.
    """
    repository = Repository(settings.database_path)
    fake = FakeRunner()
    control = ControlPlane(settings, repository, fake)  # type: ignore[arg-type]
    _seed_proxied_record(control)

    settings.generated_vars_path.parent.mkdir(parents=True, exist_ok=True)
    settings.generated_vars_path.write_text("in-flight deploy\n", encoding="utf-8")

    assert control.deployments.validate() == []

    assert settings.generated_vars_path.read_text(encoding="utf-8") == (
        "in-flight deploy\n"
    )
    scratch = fake.validated[0]
    assert scratch != settings.generated_vars_path
    # Rendered for the run and removed with it, rather than accumulating.
    assert not scratch.exists()


def test_overlapping_runs_never_share_a_variables_file(settings, monkeypatch):
    """Purge, stats and decommission all skip the deployment lock.

    They therefore overlap routinely. A fixed filename per playbook made the
    document shared mutable state: the second writer won, so a two-URL purge
    could find `purge_all: true` in the file by the time its own playbook read
    it and empty the cache on every edge.
    """
    from blitzecdn.infrastructure import ansible

    settings.cache_purge_playbook_path.write_text(
        "- hosts: blitzecdn_edges\n  tasks: []\n", encoding="utf-8"
    )
    runner = ansible.AnsibleRunner(settings, FakeEdgeStore())
    seen: list[dict[str, object]] = []

    def capture(command, *, timeout, playbook, targeted=()):
        path = Path(next(arg for arg in command if arg.startswith("@"))[1:])
        seen.append(yaml.safe_load(path.read_text(encoding="utf-8")))
        return _purge_run()

    monkeypatch.setattr(runner, "_execute", capture)

    runner.run_cache_purge(
        entries=[{"host": "cdn.example.com", "uri": "/a.js", "scheme": "http"}],
        purge_all=False,
    )
    runner.run_cache_purge(entries=[], purge_all=True)

    assert seen[0]["blitzecdn_cache_purge_all"] is False
    assert seen[1]["blitzecdn_cache_purge_all"] is True
    # Nothing is left behind for a later run to pick up.
    assert list((settings.state_dir / "runs").iterdir()) == []


def test_a_collection_reads_only_its_own_run(settings):
    """Two overlapping collections cannot answer each other's questions.

    They used to share one controller-side directory that each emptied before
    collecting, so a run could wipe the other's reports and read the whole fleet
    as silent. Counters now arrive attached to the run that asked for them, so
    the failure has no way to occur — this pins that property rather than the
    directory bookkeeping that used to approximate it.
    """
    repository = Repository(settings.database_path)
    fake = FakeRunner(
        [
            ansible_run(
                _reporting("edge-a", [{"site": "a", "outcome": "HIT", "requests": 1}])
            ),
            ansible_run(
                _reporting("edge-a", [{"site": "a", "outcome": "HIT", "requests": 99}]),
                _reporting("edge-b", [{"site": "a", "outcome": "MISS", "requests": 5}]),
            ),
        ]
    )
    control = ControlPlane(settings, repository, fake)  # type: ignore[arg-type]

    first = control.cache.cache_stats("alice")
    second = control.cache.cache_stats("alice")

    assert first.requests == 1
    assert [edge.host for edge in first.reporting] == ["edge-a"]
    assert second.requests == 104
    assert [edge.host for edge in second.reporting] == ["edge-a", "edge-b"]
    # Nothing on the controller's filesystem is involved any more.
    assert not (settings.state_dir / "stats").exists()
