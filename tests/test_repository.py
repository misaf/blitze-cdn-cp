import sqlite3
from datetime import UTC, datetime

import pytest
from sqlalchemy.pool import NullPool, QueuePool

from blitzecdn.domain.deployments import DeploymentStatus
from blitzecdn.domain.dns import DnsRecord, Domain
from blitzecdn.domain.operations import WorkflowKind, WorkflowStatus, WorkflowStep
from blitzecdn.domain.sites import CdnSite
from blitzecdn.domain.snapshots import decode_snapshot
from blitzecdn.exceptions import ConflictError, NotFoundError
from blitzecdn.infrastructure.database import Repository


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


def test_site_crud_and_audit(settings, site_payload):
    repository = Repository(settings.database_path)
    site = CdnSite.model_validate(site_payload)
    assert repository.sites.create_site(site) == site
    with pytest.raises(ConflictError):
        repository.sites.create_site(site)
    changed = site.model_copy(update={"origin_host": "192.0.2.2"})
    assert repository.sites.replace_site(changed).origin_host == "192.0.2.2"
    assert repository.sites.list_sites() == [changed]
    event = repository.audit_log.audit(
        "alice", "site.updated", "site", site.name, {"safe": True}
    )
    assert repository.audit_log.list_audit_events()[0] == event
    repository.sites.delete_site(site.name)
    with pytest.raises(NotFoundError):
        repository.sites.get_site(site.name)


def test_deployment_transitions_snapshots_and_recovery(
    settings, domain_payload, record_payload
):
    repository = Repository(settings.database_path)
    # A snapshot carries records, not the sites derived from them, so the
    # fixture has to create the canonical thing.
    repository.zones.create_domain(Domain.model_validate(domain_payload))
    repository.zones.create_record(DnsRecord.model_validate(record_payload))
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
    settings, domain_payload, record_payload
):
    repository = Repository(settings.database_path)
    repository.zones.create_domain(Domain.model_validate(domain_payload))
    repository.zones.create_record(DnsRecord.model_validate(record_payload))
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
    repository = Repository(settings.database_path)
    repository.deployment_requirements.require("certificates")
    repository.deployment_requirements.require("certificates")
    repository.close()

    reopened = Repository(settings.database_path)
    assert reopened.deployment_requirements.pending("certificates")
    reopened.deployment_requirements.clear("certificates")
    assert not reopened.deployment_requirements.pending("certificates")
    reopened.close()


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
