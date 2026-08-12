import sqlite3
from datetime import UTC, datetime

import pytest

from blitzecdn.domain.deployments import DeploymentStatus
from blitzecdn.domain.dns import DnsRecord, Domain
from blitzecdn.domain.operations import WorkflowKind, WorkflowStatus, WorkflowStep
from blitzecdn.domain.sites import CdnSite
from blitzecdn.domain.validation import STORED
from blitzecdn.exceptions import ConflictError, NotFoundError
from blitzecdn.infrastructure.database import Repository


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
    repository.zones.create_record(
        DnsRecord.model_validate(record_payload, context=STORED)
    )
    deployment = repository.deployments.create_deployment("alice", check_mode=False)
    assert (
        repository.decode_snapshot(
            repository.deployments.deployment_snapshot(deployment.id)
        )[0].name
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
    repository.zones.create_record(
        DnsRecord.model_validate(record_payload, context=STORED)
    )
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


def test_workflow_and_outbox_are_durable_and_idempotent(settings):
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

    repository.outbox.enqueue("webhook", "certificate:1", {"site": "cdn"})
    repository.outbox.enqueue("webhook", "certificate:1", {"site": "duplicate"})
    pending = repository.outbox.pending()
    assert len(pending) == 1
    repository.outbox.failed(pending[0].id, "temporarily unavailable")
    assert repository.outbox.pending()[0].attempts == 1
    repository.outbox.delivered(pending[0].id)
    assert repository.outbox.pending() == []


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
    repository.zones.replace_record(winner, expected=original)

    with pytest.raises(ConflictError, match="changed while it was being edited"):
        repository.zones.replace_record(
            original.model_copy(update={"value": "192.0.2.3"}), expected=original
        )
