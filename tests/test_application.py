from conftest import FakeRunner

from blitzecdn.application import ControlPlane
from blitzecdn.domain.models import CdnSite, DeploymentStatus, SitePatch
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
