import pytest

from blitzecdn.domain.deployments import DeploymentStatus
from blitzecdn.domain.sites import CdnSite
from blitzecdn.exceptions import ConflictError, NotFoundError
from blitzecdn.infrastructure.database import Repository


def test_site_crud_and_audit(settings, site_payload):
    repository = Repository(settings.database_path)
    site = CdnSite.model_validate(site_payload)
    assert repository.create_site(site) == site
    with pytest.raises(ConflictError):
        repository.create_site(site)
    changed = site.model_copy(update={"origin_host": "192.0.2.2"})
    assert repository.replace_site(changed).origin_host == "192.0.2.2"
    assert repository.list_sites() == [changed]
    event = repository.audit("alice", "site.updated", "site", site.name, {"safe": True})
    assert repository.list_audit_events()[0] == event
    repository.delete_site(site.name)
    with pytest.raises(NotFoundError):
        repository.get_site(site.name)


def test_deployment_transitions_snapshots_and_recovery(settings, site_payload):
    repository = Repository(settings.database_path)
    repository.create_site(CdnSite.model_validate(site_payload))
    deployment = repository.create_deployment("alice", check_mode=False)
    assert (
        repository.decode_snapshot(repository.deployment_snapshot(deployment.id))[
            0
        ].name
        == "cdn-example-com"
    )
    running = repository.transition(
        deployment.id, DeploymentStatus.QUEUED, DeploymentStatus.RUNNING
    )
    assert running.status is DeploymentStatus.RUNNING
    # A lawful step against a stale expected state is a race, so the loser gets
    # a ConflictError rather than a ValueError.
    with pytest.raises(ConflictError):
        repository.transition(
            deployment.id, DeploymentStatus.QUEUED, DeploymentStatus.RUNNING
        )
    # A step the lifecycle does not contain is refused before any row is read.
    with pytest.raises(ValueError, match="illegal deployment transition"):
        repository.transition(
            deployment.id, DeploymentStatus.QUEUED, DeploymentStatus.SUCCEEDED
        )
    assert repository.abandon_running() == 1
    assert repository.get_deployment(deployment.id).status is DeploymentStatus.ABANDONED


def test_rollback_target_requires_different_success(settings, site_payload):
    repository = Repository(settings.database_path)
    repository.create_site(CdnSite.model_validate(site_payload))
    current = repository.snapshot()
    deployment = repository.create_deployment("alice", check_mode=False)
    repository.transition(
        deployment.id, DeploymentStatus.QUEUED, DeploymentStatus.RUNNING
    )
    repository.transition(
        deployment.id, DeploymentStatus.RUNNING, DeploymentStatus.SUCCEEDED
    )
    with pytest.raises(NotFoundError):
        repository.successful_rollback_target(current)
