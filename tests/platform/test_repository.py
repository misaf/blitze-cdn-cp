import sqlite3
from datetime import UTC, datetime

import pytest
from sqlalchemy.pool import NullPool, QueuePool

from blitzecdn.capabilities.deployments.domain import (
    DeploymentRequirementKind,
    DeploymentStatus,
)
from blitzecdn.capabilities.deployments.domain.snapshots import decode_snapshot
from blitzecdn.capabilities.dns.domain import DnsRecord, Domain
from blitzecdn.capabilities.sites.domain import CdnSite
from blitzecdn.composition import Repository
from blitzecdn.core.domain.operations import WorkflowKind, WorkflowStatus, WorkflowStep
from blitzecdn.core.exceptions import ConflictError, NotFoundError


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


def test_the_site_projection_is_only_ever_rewritten_wholesale(settings, site_payload):
    """`replace_all_sites` is the store's whole write side, and deliberately.

    A projection with per-row create, update and delete invites a caller to
    edit a site that the next record change would silently re-derive over. The
    store offers no such call, so the round trip worth holding is the one the
    derivation performs: whatever it hands over is what the table then holds,
    and a name it stops producing stops existing.
    """
    repository = Repository(settings.database_path)
    site = CdnSite.model_validate(site_payload)

    repository.sites.replace_all_sites([site])
    assert repository.sites.list_sites() == [site]

    moved = site.model_copy(update={"origin_host": "192.0.2.2"})
    repository.sites.replace_all_sites([moved])
    assert repository.sites.list_sites() == [moved], "a rewrite replaces, not merges"

    repository.sites.replace_all_sites([])
    with pytest.raises(NotFoundError):
        repository.sites.get_site(site.name)


def test_audit_events_are_read_back_in_the_order_they_happened(settings, site_payload):
    repository = Repository(settings.database_path)
    site = CdnSite.model_validate(site_payload)

    event = repository.audit_log.audit(
        "alice", "site.updated", "site", site.name, {"safe": True}
    )

    assert repository.audit_log.list_audit_events()[0] == event


def _seed(repository, domain_payload, record_payload, site_payload, **policy):
    """Zone, site, record — the three rows a snapshot is made of.

    Store-level, so it deliberately goes through the stores rather than the
    services: the point of these tests is what survives a round trip through
    SQLite, not what the services do on the way.
    """
    repository.zones.create_domain(Domain.model_validate(domain_payload))
    repository.sites.create_site(
        CdnSite.model_validate({**site_payload, "server_names": [], **policy})
    )
    repository.zones.create_record(DnsRecord.model_validate(record_payload))
    repository.sites.set_server_names(
        site_payload["name"], (DnsRecord.model_validate(record_payload).fqdn,)
    )
    return repository.sites.get_site(site_payload["name"])


def test_visitor_headers_survive_persistence_and_the_snapshot(
    settings, domain_payload, record_payload, site_payload
):
    """The block lives in the `policy` JSON column, not in a column of its own.

    Nothing queries inside it, so the only way it can be lost is a round trip
    that drops what it does not recognise. This is that round trip: site in,
    site out, and the snapshot the deployment actually converges.
    """
    repository = Repository(settings.database_path)
    _seed(
        repository,
        domain_payload,
        record_payload,
        site_payload,
        visitor_headers={"connecting_ip": False, "ip_country": True},
    )

    stored = repository.sites.get_site(site_payload["name"])
    assert stored.visitor_headers.connecting_ip is False
    assert stored.visitor_headers.ip_country is True

    (site,) = decode_snapshot(repository.snapshot())
    assert site.visitor_headers == stored.visitor_headers
    assert site.requires_geoip is True


def test_under_attack_mode_survives_policy_json_and_old_rows_default_off(
    settings, domain_payload, record_payload, site_payload
):
    repository = Repository(settings.database_path)
    _seed(
        repository, domain_payload, record_payload, site_payload, under_attack_mode=True
    )
    assert repository.sites.get_site(site_payload["name"]).under_attack_mode is True
    assert decode_snapshot(repository.snapshot())[0].under_attack_mode is True

    connection = sqlite3.connect(settings.database_path)
    try:
        connection.execute(
            "UPDATE sites SET policy = json_remove(policy, '$.under_attack_mode')"
        )
        connection.commit()
    finally:
        connection.close()
    assert repository.sites.get_site(site_payload["name"]).under_attack_mode is False


def test_deployment_transitions_snapshots_and_recovery(
    settings, domain_payload, record_payload, site_payload
):
    repository = Repository(settings.database_path)
    _seed(repository, domain_payload, record_payload, site_payload)
    deployment = repository.deployments.create_deployment("alice", check_mode=False)
    assert (
        decode_snapshot(repository.deployments.deployment_snapshot(deployment.id))[
            0
        ].name
        == "cdn-example-com"
    )
    running = repository.deployments.transition(
        deployment.id, DeploymentStatus.QUEUED, DeploymentStatus.RUNNING
    )
    assert running.status is DeploymentStatus.RUNNING
    # A lawful step against a stale expected state is a race, so the loser gets
    # a ConflictError rather than a ValueError.
    with pytest.raises(ConflictError):
        repository.deployments.transition(
            deployment.id, DeploymentStatus.QUEUED, DeploymentStatus.RUNNING
        )
    # A step the lifecycle does not contain is refused before any row is read.
    with pytest.raises(ValueError, match="illegal deployment transition"):
        repository.deployments.transition(
            deployment.id, DeploymentStatus.QUEUED, DeploymentStatus.SUCCEEDED
        )
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
    current = repository.snapshot()
    deployment = repository.deployments.create_deployment("alice", check_mode=False)
    repository.deployments.transition(
        deployment.id, DeploymentStatus.QUEUED, DeploymentStatus.RUNNING
    )
    repository.deployments.transition(
        deployment.id, DeploymentStatus.RUNNING, DeploymentStatus.SUCCEEDED
    )
    with pytest.raises(NotFoundError):
        repository.deployments.successful_rollback_target(current)


def test_snapshot_reads_every_table_in_one_transaction(settings, monkeypatch):
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

    repository.snapshot()

    assert len(connections) == 2
    assert len(set(connections)) == 1


def test_transactions_never_leak_between_repository_instances(settings):
    """An ambient session is scoped to its database, not merely its context."""
    first = Repository(settings.database_path)
    second = Repository(settings.database_path.with_name("other-control-plane.db"))

    with first.transaction():
        first.zones.create_domain(Domain(name="first.example"))
        second.zones.create_domain(Domain(name="second.example"))

    assert [domain.name for domain in first.zones.list_domains()] == ["first.example"]
    assert [domain.name for domain in second.zones.list_domains()] == ["second.example"]


def test_workflow_progress_is_durable(settings):
    repository = Repository(settings.database_path)
    workflow = repository.workflows.create(
        "workflow-1", WorkflowKind.CERTIFICATE, "alice", "cdn-example-com"
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
    """The enum is the in-process type; the column keeps the string it always had.

    Typing the kind was a refactor of the callers, not of the schema. A row
    written before that change has to still be found by the enum, so the stored
    representation is asserted directly rather than only round-tripped.
    """
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
        DnsRecord(domain="example.com", name="cdn", value="192.0.2.1")
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

    These rows are published to every host at inventory precedence, so an
    unprefixed name could reach a connection variable and a reserved one could
    override what the plugin derives per edge. Enforced here so the CLI is not
    the only thing standing between a caller and the fleet.
    """
    repository = Repository(settings.database_path)

    with pytest.raises(ValueError, match=message):
        repository.ansible_settings.set_setting(name, "anything")

    assert repository.ansible_settings.list_settings() == {}


def test_history_retention_drops_drift_runs_and_keeps_rollback_targets(settings):
    """The table grows by a full desired state every hour otherwise.

    Each row carries a complete copy of every zone and record, and the drift
    timer writes one hourly whether or not anything changed. Only check-mode
    rows are prunable: a real deployment is what `successful_rollback_target`
    chooses from, so removing one could take away the snapshot the fleet needs
    to go back to.
    """
    repository = Repository(settings.database_path)
    real = [
        repository.deployments.create_deployment("alice", check_mode=False)
        for _ in range(3)
    ]
    for _ in range(10):
        repository.deployments.create_deployment("scheduler", check_mode=True)

    assert repository.deployments.prune_history(4) == 6

    kept = repository.deployments.list_deployments(limit=100)
    assert len([row for row in kept if row.check_mode]) == 4
    assert {row.id for row in real} <= {row.id for row in kept}

    # Nothing left to do on a second pass.
    assert repository.deployments.prune_history(4) == 0


def test_workflow_retention_never_drops_an_unfinished_one(settings):
    """An unfinished workflow is the record of ambiguous external work."""
    repository = Repository(settings.database_path)
    for index in range(6):
        workflow = repository.workflows.create(
            f"workflow-{index}", WorkflowKind.CERTIFICATE, "alice", "cdn-example-com"
        )
        if index < 4:
            repository.workflows.advance(workflow.id, WorkflowStatus.SUCCEEDED)

    assert repository.workflows.prune_finished(2) == 2

    remaining = {row.id for row in repository.workflows.list_workflows(100)}
    assert {row.id for row in repository.workflows.unfinished()} <= remaining
    assert len(repository.workflows.unfinished()) == 2


def test_a_compare_and_swap_outside_a_transaction_is_refused(settings):
    """The obligation the comparison rests on, enforced rather than documented.

    Outside a Unit of Work the surrounding transaction is deferred, so the read
    and the write are not one atomic step and a lost update becomes possible in
    the window between them. Refused up front, because the alternative failure
    is a SQLite snapshot-busy error surfacing somewhere unrelated.
    """
    repository = Repository(settings.database_path)
    repository.zones.create_domain(Domain(name="example.com"))
    original = repository.zones.create_record(
        DnsRecord(domain="example.com", name="cdn", value="192.0.2.1")
    )

    with pytest.raises(ValueError, match="must run inside a Unit of Work"):
        repository.zones.replace_record(
            original.model_copy(update={"value": "192.0.2.9"}), expected=original
        )

    # Without `expected` there is nothing to compare, so no transaction is
    # required and an ordinary write still works.
    repository.zones.replace_record(original.model_copy(update={"value": "192.0.2.9"}))
