import sqlite3
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from control_plane_fixtures import (
    FakeRunner,
    RecordingBackgroundQueue,
    ansible_run,
    host_run,
    seed_site,
)

from blitzecdn.capabilities.deployments.domain import DeploymentStatus
from blitzecdn.capabilities.dns.domain import (
    CdnSite,
    DnsRecord,
    Domain,
    DomainPatch,
    RecordPatch,
    RecordType,
    Rule,
)
from blitzecdn.capabilities.workflows.domain import WorkflowStatus
from blitzecdn.composition import ControlPlane, Repository
from blitzecdn.core.domain.runs import RunStatus
from blitzecdn.core.exceptions import (
    DeploymentBusyError,
)


def _seed_proxied_record(control: ControlPlane) -> CdnSite:
    """The zone, site and record most tests need: `example-com`."""
    return seed_site(control)


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


def test_the_audit_retention_setting_reaches_the_log_that_enforces_it(settings):
    """The store honours a bound; this is that the composition hands it one.

    `AuditLog` carries a default so a test can build one without an opinion,
    which is exactly the shape that lets a setting silently go unread — the
    control plane would keep a hundred thousand events whatever an operator
    configured, and nothing would say so.
    """
    control = ControlPlane(settings=settings.model_copy(update={"audit_retention": 4}))
    try:
        for index in range(12):
            control.events.audit("alice", f"a{index}", "site", "s")
        kept = control.audit.list_audit_events()
    finally:
        control.close()

    assert [event.action for event in kept] == ["a11", "a10", "a9", "a8"]


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
        runner=FakeRunner(),
    )
    control.dns.create_domain(Domain(name="example.com"), "alice")

    def refuse_event(_event):
        raise RuntimeError("audit recorder failed")

    # Event recording participates in the same unit of work as the record and
    # the hostname projection, so any recorder failure must roll everything back.
    monkeypatch.setattr(repository.audit_log, "record", refuse_event)
    with pytest.raises(RuntimeError, match="audit recorder failed"):
        control.dns.create_record(
            DnsRecord(
                domain="example.com", name="cdn", value="198.51.100.10", proxied=True
            ),
            "alice",
        )

    assert repository.zones.list_records() == []
    assert control.dns.list_sites() == []
    assert sorted(
        event.action for event in repository.audit_log.list_audit_events()
    ) == ["domain.created"]


def test_the_hostnames_an_edge_serves_cannot_drift_from_the_records(settings):
    """The test this replaces existed because a table restated the records.

    ``CdnSite.server_names`` was a projection with a revision stamp beside it,
    ``validation_errors`` reported it stale, and ``rebuild_hostname_projection``
    repaired it. All three are gone: the hostnames are computed from the
    records on every read, so there is no second copy to fall behind.

    What is left to assert is that it really is computed — that a record
    written behind the service's back still shows up.
    """
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings, repository=repository, runner=FakeRunner()
    )
    _seed_proxied_record(control)

    repository.zones.create_record(
        DnsRecord(
            domain="example.com",
            name="www",
            value="198.51.100.10",
            proxied=True,
        )
    )

    (site,) = control.dns.list_sites()
    assert site.server_names == ("cdn.example.com", "www.example.com")
    assert control.dns.validation_errors() == []
    assert not hasattr(control.dns, "rebuild_hostname_projection")


def test_external_deployment_run_never_holds_a_database_transaction(settings):
    repository = Repository(settings.database_path)

    class TransactionAwareRunner(FakeRunner):
        def run(self, *, check: bool, host_limit: str | None = None):
            assert getattr(repository.database._local, "connection", None) is None
            return super().run(check=check, host_limit=host_limit)

    control = ControlPlane(
        settings=settings, repository=repository, runner=TransactionAwareRunner()
    )
    control.deployments.deploy("alice")


def test_crud_validate_and_successful_deploy(settings):
    repository = Repository(settings.database_path)
    runner = FakeRunner([ansible_run(host_run("edge-a")) for _ in range(2)])
    control = ControlPlane(settings=settings, repository=repository, runner=runner)
    site = seed_site(control, name="example-com", record="cdn")
    control.dns.update_domain(
        "example.com", DomainPatch(cache_enabled=False, compression="off"), "alice"
    )
    assert control.dns.get_site(site.name).cache_enabled is False
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
    )

    assert control.deployments.deploy("alice").status is DeploymentStatus.SUCCEEDED
    desired = settings.generated_vars_path.read_text(encoding="utf-8")
    assert "blitzecdn_nginx_allow_empty_sites: false" in desired

    approved = ControlPlane(
        settings=settings.model_copy(update={"allow_empty_sites": True}),
        repository=repository,
        runner=FakeRunner(),
    )
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
    )

    with pytest.raises(KeyboardInterrupt):
        control.deployments.deploy("alice")

    deployment = repository.deployments.list_deployments(1)[0]
    assert deployment.status is DeploymentStatus.ABANDONED
    assert deployment.finished_at is not None
    assert deployment.result is not None
    assert "KeyboardInterrupt" in (deployment.result.error or "")


def test_routing_adds_and_removes_the_hostname_the_edge_serves(settings):
    """The CDN on/off switch is which site a record names, if any.

    The site survives the switch now. Unrouting used to delete the whole
    virtual host along with its policy, because the record *was* the policy;
    here it takes the hostname off a site that is still configured and waiting.
    """
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings, repository=repository, runner=FakeRunner()
    )
    seed_site(control, name="example-com", record="cdn")
    control.dns.create_record(
        DnsRecord(
            domain="example.com", name="db", proxied=False, value="198.51.100.11"
        ),
        "alice",
    )

    # Only the proxied record puts a hostname on the edge.
    assert [site.server_names for site in control.dns.list_sites()] == [
        ("cdn.example.com",)
    ]

    control.dns.unproxy("example.com", "cdn", RecordType.A, "203.0.113.7", "alice")
    # Nothing proxied, so the zone derives no virtual host at all.
    assert control.dns.list_sites() == []

    control.dns.proxy("example.com", "cdn", RecordType.A, "alice")
    assert [site.server_names for site in control.dns.list_sites()] == [
        ("cdn.example.com",)
    ]


def test_removing_a_domain_takes_its_hostnames_off_the_edge(settings):
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings, repository=repository, runner=FakeRunner()
    )
    seed_site(control, name="example-com", record="cdn")
    control.dns.delete_domain("example.com", "alice")
    assert repository.zones.list_records() == []
    # The zone went, and everything derived from it went with it.
    assert control.dns.list_sites() == []


def _plane(settings, repository):
    return ControlPlane(settings=settings, repository=repository, runner=FakeRunner())


def test_a_hostname_cannot_reach_two_origins_at_once(settings):
    """A hostname resolves once, through one policy, but its two families are
    still two records — and every record carries its own origin. Writing both a
    proxied A and a proxied AAAA is exactly how a hostname pointed two places
    gets written, including behind the service's back, which this checks.

    It cannot be served that way: one virtual host has one upstream, so
    validation refuses the pair. The A record is IPv4-only and the AAAA
    IPv6-only, so the two origins can never agree — one family has to be
    unproxied.
    """
    repository = Repository(settings.database_path)
    control = _plane(settings, repository)
    seed_site(control, name="example-com", record="www")
    repository.zones.create_record(
        DnsRecord(
            domain="example.com",
            name="www",
            type=RecordType.AAAA,
            value="2001:db8::10",
        )
    )

    errors = control.dns.validation_errors()
    assert any(
        "www.example.com" in error and "different origins" in error for error in errors
    )

    control.dns.update_record(
        "example.com", "www", RecordType.AAAA, RecordPatch(proxied=False), "alice"
    )
    (site,) = control.dns.list_sites()
    assert site.server_names == ("www.example.com",)
    assert control.dns.validation_errors() == []


def test_a_rule_claims_a_hostname_without_taking_it_from_its_zone(settings):
    """Two policies in one zone, and each hostname resolves to exactly one."""
    repository = Repository(settings.database_path)
    control = _plane(settings, repository)
    seed_site(control, name="example-com", record="www")
    control.rules.create_rule(
        Rule(
            domain="example.com",
            name="api",
            match="api.example.com",
            overrides={"cache_enabled": False},
        ),
        "alice",
    )
    control.dns.create_record(
        DnsRecord(domain="example.com", name="api", value="198.51.100.10"), "alice"
    )

    hosts = {site.name: site for site in control.dns.list_sites()}
    assert hosts["example-com"].server_names == ("www.example.com",)
    assert hosts["example-com--api"].server_names == ("api.example.com",)
    assert hosts["example-com--api"].cache_enabled is False
    assert control.dns.validation_errors() == []


def test_validate_reports_a_hostname_served_from_two_origins(settings):
    """The one contradiction canonical state can still hold.

    It replaces two that it cannot: a record naming a site that is gone, and a
    hostname routed to two sites. Both were about a stored reference between a
    record and a site, and there is no such reference now — what is left is a
    hostname whose two families point the edge at two different origins, which
    one virtual host with one upstream cannot serve.

    Written behind the service, which is what a restore from a damaged backup
    amounts to.
    """
    repository = Repository(settings.database_path)
    control = _plane(settings, repository)
    seed_site(control, name="example-com", record="www")
    connection = sqlite3.connect(settings.database_path)
    try:
        connection.execute(
            "INSERT INTO dns_records "
            "(domain, name, type, value, ttl, proxied, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?)",
            (
                "example.com",
                "www",
                "AAAA",
                "2001:db8::10",
                300,
                datetime.now(UTC).isoformat(),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    errors = control.dns.validation_errors()
    assert any(
        "www.example.com" in error and "different origins" in error for error in errors
    )


def test_a_proxied_record_may_still_be_updated_in_place(settings):
    repository = Repository(settings.database_path)
    control = _plane(settings, repository)
    seed_site(control, name="example-com", record="www")

    updated = control.dns.update_record(
        "example.com", "www", RecordType.A, RecordPatch(ttl=600), "alice"
    )

    assert updated.ttl == 600
    assert updated.proxied
    assert control.dns.get_site("example-com").server_names == ("www.example.com",)


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
    control = ControlPlane(settings=settings, repository=repository, runner=runner)
    assert control.deployments.deploy("alice").status is DeploymentStatus.FAILED
    assert (
        control.deployments.deploy("alice", check=True).status
        is DeploymentStatus.TIMED_OUT
    )
    assert runner.check_modes == [False, True]


def test_rollback_updates_canonical_state_only_after_success(settings):
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner([ansible_run(host_run("edge-a")) for _ in range(2)]),
    )
    original = seed_site(control, name="example-com", record="cdn")
    successful = control.deployments.deploy("alice")
    control.dns.update_domain("example.com", DomainPatch(cache_enabled=False), "alice")
    result = control.deployments.rollback("alice", successful.id)
    assert result.status is DeploymentStatus.SUCCEEDED
    # Rollback restores the zone the snapshot carried, so the host it derives
    # comes back with the origin, the hostnames and the policy it had.
    restored = control.dns.get_site(original.name)
    assert restored.origin_host == original.origin_host
    assert restored.server_names == ("cdn.example.com",)
    assert restored.cache_enabled is True
    assert control.dns.validation_errors() == []


def test_rollback_restoration_failure_is_atomic_and_never_reports_success(settings):
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner([ansible_run(host_run("edge-a")) for _ in range(2)]),
    )
    original = _seed_proxied_record(control)
    successful = control.deployments.deploy("alice")
    control.dns.update_domain("example.com", DomainPatch(cache_enabled=False), "alice")
    current = control.dns.get_site(original.name)

    def fail_restore(_domains, _records):
        raise RuntimeError("restore failed")

    repository.zones.replace_all_records = fail_restore
    result = control.deployments.rollback("alice", successful.id)

    assert result.status is DeploymentStatus.FAILED
    assert control.dns.get_site(original.name) == current
    assert repository.zones.list_records() != []
    actions = [event.action for event in repository.audit_log.list_audit_events(10)]
    assert "rollback.applied" not in actions
    # Only the original deployment may have announced success.
    assert actions.count("deployment.succeeded") == 1
    assert original.cache_enabled != current.cache_enabled


def test_rollback_holds_the_lock_across_the_canonical_state_swap(settings):
    """Swapping canonical state after the lock released would drop edits."""
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

    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=LockingRunner([ansible_run(host_run("edge-a")) for _ in range(2)]),
    )
    seed_site(control)

    # Recording starts after the seeding, so nothing but the rollback's own
    # wholesale restore is counted.
    original_replace = repository.zones.replace_all_records

    def recording_replace(domains, records):
        events.append("state-replaced")
        original_replace(domains, records)

    repository.zones.replace_all_records = recording_replace

    successful = control.deployments.deploy("alice")
    # A concurrent edit while the fleet converges. The rollback restores the
    # snapshot's zones over it, and must do so while still holding the lock —
    # swapping them after releasing it would drop whatever landed in between
    # without the guard ever seeing it.
    control.dns.update_domain("example.com", DomainPatch(cache_enabled=False), "bob")

    control.deployments.rollback("alice", successful.id)

    assert events == [
        "locked",
        "unlocked",  # the initial deploy
        "locked",
        "state-replaced",
        "unlocked",
    ]


def test_a_stopped_fleet_deploy_names_the_edges_it_never_reached(settings):
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
    )
    seed_site(control)

    deployment = control.deployments.deploy("alice")

    assert deployment.status is DeploymentStatus.FAILED
    assert deployment.unattempted == ("edge-c", "edge-d")
    # And it reaches the operator, rather than only being available to ask for.
    assert "edge-c, edge-d" in (deployment.detail or "")
    assert "never attempted" in (deployment.detail or "")


def test_a_drift_check_that_stopped_early_is_not_in_sync(settings):
    """An edge the check never got to is one we have no answer for."""
    repository = Repository(settings.database_path)
    partial = ansible_run(
        host_run("edge-a", changed=0),
        targeted=("edge-a", "edge-b"),
    )
    control = ControlPlane(
        settings=settings, repository=repository, runner=FakeRunner([partial])
    )
    seed_site(control)

    report = control.deployments.check_drift("alice")

    assert report.unattempted == ("edge-b",)
    assert report.in_sync is False


def test_startup_recovery_abandons_what_a_dead_process_left_behind(settings):
    """The case startup recovery exists for: nobody is deploying."""
    repository = Repository(settings.database_path)
    queue = RecordingBackgroundQueue()
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner(),
        background=queue,
    )
    with repository.transaction():
        stranded = repository.deployments.create_deployment("alice", check_mode=False)
        repository.deployments.transition(
            stranded.id, DeploymentStatus.QUEUED, DeploymentStatus.RUNNING
        )
        workflow = repository.workflows.create(
            "interrupted", "certificate", "alice", "example-com"
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
    )
    live = repository.deployments.create_deployment("alice", check_mode=False)
    workflow = repository.workflows.create(
        "live", "certificate", "alice", "example-com"
    )
    repository.workflows.advance(workflow.id, WorkflowStatus.RUNNING)

    assert control.deployments.initialize() == 0
    assert (
        repository.deployments.get_deployment(live.id).status is DeploymentStatus.QUEUED
    )
    assert repository.workflows.get(workflow.id).status is WorkflowStatus.RUNNING


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
    )
    _seed_proxied_record(control)
    successful = control.deployments.deploy("alice")

    concurrent = DnsRecord(
        domain="example.com", name="late", proxied=False, value="198.51.100.77"
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
    )
    original = _seed_proxied_record(control)
    successful = control.deployments.deploy("alice")
    control.dns.update_domain("example.com", DomainPatch(cache_enabled=False), "alice")

    rolled_back = control.deployments.rollback("alice", successful.id)

    assert rolled_back.status is DeploymentStatus.SUCCEEDED
    assert control.dns.get_site(original.name) == original
