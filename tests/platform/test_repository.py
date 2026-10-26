import sqlite3
from datetime import UTC, datetime

import pytest
from sqlalchemy.pool import NullPool, QueuePool
from support.control_plane import advance, build_application
from support.policy import domain_from_settings, site_from_settings

from blitzecdn.capabilities.deployments.domain import (
    DeploymentRequirementKind,
    DeploymentStatus,
)
from blitzecdn.capabilities.dns.domain import (
    DnsRecord,
    Domain,
    RecordType,
    derive_hosts,
)
from blitzecdn.capabilities.edges.domain import Edge
from blitzecdn.capabilities.releases.service import compile_release
from blitzecdn.capabilities.releases.service.preparation import prepare_compilation
from blitzecdn.capabilities.workflows.domain import (
    WorkflowStatus,
    WorkflowStep,
)
from blitzecdn.composition import Repository
from blitzecdn.core.exceptions import ConflictError, NotFoundError
from blitzecdn.core.persistence.schema import DatabaseSchema


def test_database_does_not_retain_idle_connections(settings):
    repository = Repository(settings.database_path)

    assert isinstance(repository.database.engine.pool, NullPool)


def test_database_uses_a_bounded_pool_for_a_long_lived_server(settings):
    repository = Repository(settings.database_path, pool_connections=True)

    assert isinstance(repository.database.engine.pool, QueuePool)
    assert repository.database.engine.pool.size() == 5

    repository.close()


def test_repository_close_disposes_the_engine(settings, monkeypatch):
    repository = Repository(settings.database_path)
    disposed = False

    def dispose() -> None:
        nonlocal disposed
        disposed = True

    monkeypatch.setattr(repository.database.engine, "dispose", dispose)

    repository.close()

    assert disposed


def test_the_virtual_hosts_are_a_function_of_the_rows_behind_them(settings):
    """Virtual hosts are derived from zones, rules and records on every read.

    Change a row and the hosts change; stop producing a name and it stops
    existing. Nothing can write a host directly.
    """
    repository = Repository(settings.database_path)
    control = build_application(settings=settings, repository=repository)
    repository.zones.create_domain(Domain(name="example.com"))
    repository.zones.create_record(
        DnsRecord(domain="example.com", name="cdn", value="192.0.2.1")
    )

    (site,) = control.sites.list_sites()
    assert site.name == "example-com"
    assert site.origin_host == "192.0.2.1"

    repository.zones.replace_record(
        DnsRecord(domain="example.com", name="cdn", value="192.0.2.2")
    )
    assert control.sites.list_sites()[0].origin_host == "192.0.2.2"

    repository.zones.delete_record("example.com", "cdn", RecordType.A)
    with pytest.raises(NotFoundError):
        control.sites.get_site("example-com")

    assert not any(
        "site" in name
        for name in vars(type(repository.zones))
        if not name.startswith("_")
    )


def test_audit_events_are_read_back_in_the_order_they_happened(settings, site_payload):
    repository = Repository(settings.database_path)
    site = site_from_settings(site_payload)

    event = repository.audit_log.audit(
        "alice", "site.updated", "site", site.name, {"safe": True}
    )

    assert repository.audit_log.list_audit_events()[0] == event


def test_the_audit_log_keeps_only_its_retention(settings):
    """The one table written on every mutation, and bounded.

    The count stops climbing and the newest entries survive.
    """
    repository = Repository(settings.database_path, audit_retention=5)

    for index in range(20):
        repository.audit_log.audit("alice", f"site.updated.{index}", "site", "s")

    kept = repository.audit_log.list_audit_events()
    assert len(kept) == 5
    assert [event.action for event in kept] == [
        f"site.updated.{index}" for index in range(19, 14, -1)
    ]


def test_pruning_the_audit_log_leaves_the_ids_of_survivors_alone(settings):
    """An id is what `get_audit_event` is called with, so it has to stay put."""
    repository = Repository(settings.database_path, audit_retention=3)

    for index in range(10):
        repository.audit_log.audit("alice", f"a{index}", "site", "s")

    kept = repository.audit_log.list_audit_events()
    for event in kept:
        assert repository.audit_log.get_audit_event(event.id) == event
    assert [event.id for event in kept] == [10, 9, 8]


def test_an_audit_log_under_its_retention_loses_nothing(settings):
    """The common case: the delete runs on every append and finds no row."""
    repository = Repository(settings.database_path, audit_retention=1000)

    for index in range(25):
        repository.audit_log.audit("alice", f"a{index}", "site", "s")

    assert len(repository.audit_log.list_audit_events()) == 25


def _release(repository):
    """Compile and record a release from whatever these rows currently say.

    The compiler is pure, so no control plane is needed. Exercises the foreign
    key: a deployment names a release that must exist.
    """
    inputs = repository.release_inputs()
    # Whatever these rows happen to ask for; store tests are not about installed
    # capabilities.
    release = compile_release(
        prepare_compilation(
            inputs,
            capabilities={
                token
                for host in _hosts(repository)
                for token in host.policy.required_capabilities
            },
            targets=(),
            contributors=_NoContributions(),
            site_checks=lambda site: (),
            allow_empty_sites=False,
        )
    )
    with repository.transaction():
        return repository.releases.save(release, inputs)


class _NoContributions:
    """An installation whose capabilities contribute nothing to the document."""

    def site_variables(self, site):
        return {"blitzecdn_site_name": site.name}

    def fleet_variables(self, sites):
        return {}


def _seed(repository, domain_payload, record_payload, site_payload, **policy):
    """Zone and record: the two rows release inputs are made of.

    Through the stores, since these tests are about the SQLite round trip. The
    origin is the record's ``value``.
    """
    repository.zones.create_domain(domain_from_settings({**domain_payload, **policy}))
    repository.zones.create_record(DnsRecord.model_validate(record_payload))
    return _hosts(repository)[0]


def _hosts(repository):
    """The virtual hosts these rows derive, read the way a deploy reads them."""
    return derive_hosts(
        repository.zones.list_domains(),
        repository.rules.list_rules(),
        repository.zones.list_records(),
    )


def test_visitor_headers_survive_persistence_and_compilation(
    settings, domain_payload, record_payload, site_payload
):
    """The block lives in the `policy` JSON column, not in a column of its own.

    So it can only be lost by a round trip that drops unknown keys.
    """
    repository = Repository(settings.database_path)
    _seed(
        repository,
        domain_payload,
        record_payload,
        site_payload,
        visitor_headers={"connecting_ip": False, "ip_country": True},
    )

    (stored,) = _hosts(repository)
    assert stored.policy.headers.visitor_headers.connecting_ip is False
    assert stored.policy.headers.visitor_headers.ip_country is True

    (compiled,) = _release(repository).sites
    assert (
        compiled.site.policy.headers.visitor_headers
        == stored.policy.headers.visitor_headers
    )
    assert compiled.site.policy.requires_geoip is True


def test_under_attack_mode_survives_nested_policy_json_and_defaults_when_absent(
    settings, domain_payload, record_payload, site_payload
):
    repository = Repository(settings.database_path)
    _seed(
        repository, domain_payload, record_payload, site_payload, under_attack_mode=True
    )
    assert _hosts(repository)[0].policy.security.under_attack_mode is True
    assert _release(repository).sites[0].site.policy.security.under_attack_mode is True

    connection = sqlite3.connect(settings.database_path)
    try:
        connection.execute(
            "UPDATE domains SET policy = "
            "json_remove(policy, '$.security.under_attack_mode')"
        )
        connection.commit()
    finally:
        connection.close()
    assert _hosts(repository)[0].policy.security.under_attack_mode is False


def test_deployment_transitions_releases_and_recovery(
    settings, domain_payload, record_payload, site_payload
):
    repository = Repository(settings.database_path)
    _seed(repository, domain_payload, record_payload, site_payload)
    release = _release(repository)
    deployment = repository.deployments.create_deployment(
        "alice", release_id=release.id, check_mode=False
    )
    assert repository.deployments.deployment_release(deployment.id) == release.id
    assert [entry.name for entry in release.sites] == ["example-com"]
    now = datetime.now(UTC)
    running = repository.deployments.save(deployment, deployment.start(now=now))
    assert running.status is DeploymentStatus.RUNNING
    assert running.started_at == now
    # A lawful step against a stale state is a race, so the loser gets a
    # ConflictError rather than a ValueError.
    with pytest.raises(ConflictError):
        repository.deployments.save(deployment, deployment.start(now=now))
    # A step the lifecycle does not contain is refused by the domain, before
    # anything reaches the store.
    with pytest.raises(ValueError, match="illegal deployment transition"):
        deployment.finish(DeploymentStatus.SUCCEEDED, now=now)
    with pytest.raises(ValueError, match="not a finished state"):
        running.finish(DeploymentStatus.RUNNING, now=now)
    assert repository.deployments.abandon_running() == 1
    assert (
        repository.deployments.get_deployment(deployment.id).status
        is DeploymentStatus.ABANDONED
    )


def test_rollback_target_requires_different_success(
    settings, domain_payload, record_payload, site_payload
):
    repository = Repository(settings.database_path)
    _seed(repository, domain_payload, record_payload, site_payload)
    current = _release(repository)
    deployment = repository.deployments.create_deployment(
        "alice", release_id=current.id, check_mode=False
    )
    advance(repository.deployments, deployment.id, DeploymentStatus.RUNNING)
    advance(repository.deployments, deployment.id, DeploymentStatus.SUCCEEDED)
    with pytest.raises(NotFoundError):
        repository.deployments.successful_rollback_target(current.id)


def test_release_inputs_read_every_table_in_one_transaction(settings, monkeypatch):
    repository = Repository(settings.database_path)
    connections: list[int] = []

    def observe(method):
        def wrapped(*args, **kwargs):
            session = repository.database.current_session()
            assert session is not None
            connections.append(id(session))
            return method(*args, **kwargs)

        return wrapped

    monkeypatch.setattr(
        repository.zones, "list_domains", observe(repository.zones.list_domains)
    )
    monkeypatch.setattr(
        repository.zones, "list_records", observe(repository.zones.list_records)
    )

    repository.release_inputs()

    assert len(connections) == 2
    assert len(set(connections)) == 1


def test_transactions_never_leak_between_repository_instances(settings):
    """An ambient session is scoped to its database, not merely its context."""
    first = Repository(settings.database_path)
    other = settings.database_path.with_name("other-control-plane.db")
    DatabaseSchema.migrate(other)
    second = Repository(other)

    with first.transaction():
        first.zones.create_domain(Domain(name="first.example"))
        second.zones.create_domain(Domain(name="second.example"))

    assert [domain.name for domain in first.zones.list_domains()] == ["first.example"]
    assert [domain.name for domain in second.zones.list_domains()] == ["second.example"]


def test_failed_nested_use_case_rolls_back_every_store_and_resets_the_session(settings):
    repository = Repository(settings.database_path)

    with pytest.raises(RuntimeError, match="nested failure"), repository.transaction():
        repository.zones.create_domain(Domain(name="example.com"))
        with repository.transaction():
            repository.audit_log.audit("alice", "domain.added", "domain", "example.com")
            raise RuntimeError("nested failure")

    assert repository.database.current_session() is None
    assert repository.zones.list_domains() == []
    assert repository.audit_log.list_audit_events() == []
    with repository.transaction():
        repository.zones.create_domain(Domain(name="retry.example"))
    assert repository.zones.get_domain("retry.example").name == "retry.example"


def test_workflow_progress_is_durable(settings):
    repository = Repository(settings.database_path)
    workflow = repository.workflows.create(
        "workflow-1", "certificate", "alice", "example-com"
    )
    assert workflow.status is WorkflowStatus.PENDING
    repository.workflows.advance(
        workflow.id,
        WorkflowStatus.RUNNING,
        step=WorkflowStep(name="issued", completed_at=datetime.now(UTC)),
    )
    assert repository.workflows.unfinished()[0].steps[0].name == "issued"


def test_deployment_requirements_are_durable_and_idempotent(settings):
    kind = DeploymentRequirementKind.CERTIFICATES
    repository = Repository(settings.database_path)
    repository.deployment_requirements.require(kind)
    repository.deployment_requirements.require(kind)
    repository.close()

    reopened = Repository(settings.database_path)
    assert reopened.deployment_requirements.pending(kind)
    reopened.deployment_requirements.clear(kind)
    assert not reopened.deployment_requirements.pending(kind)
    reopened.close()


def test_a_requirement_is_stored_under_its_enum_value(settings):
    """The column stores the enum's string value, asserted directly."""
    repository = Repository(settings.database_path)
    repository.deployment_requirements.require(DeploymentRequirementKind.CERTIFICATES)
    connection = sqlite3.connect(settings.database_path)
    try:
        stored = connection.execute(
            "SELECT kind FROM deployment_requirements"
        ).fetchall()
    finally:
        connection.close()
    repository.close()

    assert stored == [("certificates",)]


def test_begin_immediate_reserves_the_cross_process_writer(settings):
    repository = Repository(settings.database_path)
    other = sqlite3.connect(settings.database_path, timeout=0.01)
    try:
        with (
            repository.transaction(),
            pytest.raises(sqlite3.OperationalError, match="locked"),
        ):
            other.execute("BEGIN IMMEDIATE")
    finally:
        other.close()


def test_record_updates_detect_a_stale_expected_version(settings):
    repository = Repository(settings.database_path)
    repository.zones.create_domain(Domain(name="example.com"))
    original = repository.zones.create_record(
        DnsRecord(domain="example.com", name="cdn", proxied=False, value="192.0.2.1")
    )
    winner = original.model_copy(update={"value": "192.0.2.2"})
    # Under a Unit of Work, as the services do it. The comparison is only
    # atomic inside one, and the store now refuses it outside one.
    with repository.transaction():
        repository.zones.replace_record(winner, expected=original)

    with (
        pytest.raises(ConflictError, match="changed while it was being edited"),
        repository.transaction(),
    ):
        repository.zones.replace_record(
            original.model_copy(update={"value": "192.0.2.3"}), expected=original
        )


def test_edge_updates_detect_a_stale_expected_version(settings):
    """The edge store's compare-and-swap.

    `EdgeOperationsService.update_edge` writes back with `expected=` the copy it
    read, so a racing edit is refused. Against the real store, since the read,
    comparison and write must happen inside one `BEGIN IMMEDIATE`.
    """
    repository = Repository(settings.database_path)
    original = repository.edges.create_edge(
        Edge.model_validate({"name": "edge-a", "host": "edge-a.example.net"})
    )
    winner = original.model_copy(update={"host": "moved.example.net"})
    # Under a Unit of Work, as `update_edge` does it: the store refuses the
    # comparison outside one, because only there is it atomic.
    with repository.transaction():
        repository.edges.replace_edge(winner, expected=original)

    # The loser still holds the copy it read before the winner landed.
    with (
        pytest.raises(ConflictError, match="changed while it was being edited"),
        repository.transaction(),
    ):
        repository.edges.replace_edge(
            original.model_copy(update={"host": "stale.example.net"}),
            expected=original,
        )

    assert repository.edges.get_edge("edge-a").host == "moved.example.net"


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("ansible_port", "must start with"),
        ("blitzecdn_firewall_ssh_port", "derived from edge"),
        ("blitzecdn_api_token", "belong in .env"),
    ],
)
def test_the_store_refuses_a_setting_name_the_fleet_should_not_carry(
    settings, name, message
):
    """The rule belongs to the setting, not to the command that types it.

    These rows reach every host at inventory precedence, so an unprefixed name
    could reach a connection variable and a reserved one could override a derived
    value.
    """
    repository = Repository(settings.database_path)

    with pytest.raises(ValueError, match=message):
        repository.ansible_settings.set_setting(name, "anything")

    assert repository.ansible_settings.list_settings() == {}


@pytest.mark.parametrize(
    "finished_status",
    [
        DeploymentStatus.SUCCEEDED,
        DeploymentStatus.FAILED,
        DeploymentStatus.TIMED_OUT,
        DeploymentStatus.ABANDONED,
    ],
)
def test_history_retention_drops_drift_runs_and_keeps_rollback_targets(
    settings, finished_status
):
    """The drift timer writes a row an hour whether or not anything changed.

    Only check-mode rows are pruned; real deployments are rollback targets.
    """
    repository = Repository(settings.database_path)
    release = _release(repository)
    real = [
        repository.deployments.create_deployment(
            "alice", release_id=release.id, check_mode=False
        )
        for _ in range(3)
    ]
    for _ in range(10):
        deployment = repository.deployments.create_deployment(
            "scheduler", release_id=release.id, check_mode=True
        )
        advance(repository.deployments, deployment.id, DeploymentStatus.RUNNING)
        advance(repository.deployments, deployment.id, finished_status)

    assert repository.deployments.prune_history(4) == 6

    kept = repository.deployments.list_deployments(limit=100)
    assert len([row for row in kept if row.check_mode]) == 4
    assert {row.id for row in real} <= {row.id for row in kept}

    # Nothing left to do on a second pass.
    assert repository.deployments.prune_history(4) == 0


@pytest.mark.parametrize("running", [False, True])
def test_history_retention_preserves_unfinished_drift_progress(settings, running):
    repository = Repository(settings.database_path)
    release = _release(repository)
    unfinished = repository.deployments.create_deployment(
        "scheduler", release_id=release.id, check_mode=True
    )
    repository.deployment_targets.plan(unfinished.id, ["edge-a"], fence=0)
    if running:
        unfinished = advance(
            repository.deployments, unfinished.id, DeploymentStatus.RUNNING
        )
    for _ in range(3):
        completed = repository.deployments.create_deployment(
            "scheduler", release_id=release.id, check_mode=True
        )
        advance(repository.deployments, completed.id, DeploymentStatus.RUNNING)
        advance(repository.deployments, completed.id, DeploymentStatus.SUCCEEDED)

    assert repository.deployments.prune_history(1) == 2
    assert repository.deployments.get_deployment(unfinished.id) == unfinished
    assert [
        target.edge for target in repository.deployment_targets.targets(unfinished.id)
    ] == ["edge-a"]
    assert repository.deployments.prune_history(1) == 0


def test_workflow_retention_never_drops_an_unfinished_one(settings):
    """An unfinished workflow is the record of ambiguous external work."""
    repository = Repository(settings.database_path)
    for index in range(6):
        workflow = repository.workflows.create(
            f"workflow-{index}", "certificate", "alice", "example-com"
        )
        if index < 4:
            repository.workflows.advance(workflow.id, WorkflowStatus.SUCCEEDED)

    assert repository.workflows.prune_finished(2) == 2

    remaining = {row.id for row in repository.workflows.list_workflows(100)}
    assert {row.id for row in repository.workflows.unfinished()} <= remaining
    assert len(repository.workflows.unfinished()) == 2


def test_a_compare_and_swap_outside_a_transaction_is_refused(settings):
    """Outside a Unit of Work the read and write are not atomic, so the store
    refuses rather than risk a lost update or a snapshot-busy error.
    """
    repository = Repository(settings.database_path)
    repository.zones.create_domain(Domain(name="example.com"))
    original = repository.zones.create_record(
        DnsRecord(domain="example.com", name="cdn", proxied=False, value="192.0.2.1")
    )

    with pytest.raises(ValueError, match="must run inside a Unit of Work"):
        repository.zones.replace_record(
            original.model_copy(update={"value": "192.0.2.9"}), expected=original
        )

    # Without `expected` there is nothing to compare, so no transaction is
    # required and an ordinary write still works.
    repository.zones.replace_record(original.model_copy(update={"value": "192.0.2.9"}))
