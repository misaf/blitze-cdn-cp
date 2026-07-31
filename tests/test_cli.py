import json

from conftest import FakeRunner
from typer.testing import CliRunner

from blitzecdn import cli
from blitzecdn.application import ControlPlane
from blitzecdn.domain.models import CdnSite
from blitzecdn.infrastructure.ansible import CommandResult
from blitzecdn.infrastructure.database import Repository

runner = CliRunner()


def test_init_creates_private_environment_file(tmp_path):
    output = tmp_path / "generated.env"
    result = runner.invoke(cli.app, ["init", "--output", str(output)])
    assert result.exit_code == 0
    assert output.stat().st_mode & 0o777 == 0o600
    assert "BLITZE_API_KEYS=local:" in output.read_text(encoding="utf-8")
    duplicate = runner.invoke(cli.app, ["init", "--output", str(output)])
    assert duplicate.exit_code == 2


def test_cli_site_status_audit_and_doctor(
    settings, site_payload, monkeypatch, tmp_path
):
    control = ControlPlane(settings, Repository(settings.database_path), FakeRunner())  # type: ignore[arg-type]
    monkeypatch.setattr(cli, "_control_plane", lambda: control)
    monkeypatch.setattr(cli, "_settings", lambda: settings)
    site_file = tmp_path / "site.json"
    site_file.write_text(json.dumps(site_payload), encoding="utf-8")
    assert (
        runner.invoke(cli.app, ["site", "add", "--file", str(site_file)]).exit_code == 0
    )
    listed = runner.invoke(cli.app, ["site", "list", "--json"])
    assert listed.exit_code == 0 and "example-cdn" in listed.stdout
    assert runner.invoke(cli.app, ["doctor", "--json"]).exit_code == 0
    assert runner.invoke(cli.app, ["audit", "--json"]).exit_code == 0
    assert (
        runner.invoke(cli.app, ["site", "remove", "example-cdn", "--yes"]).exit_code
        == 0
    )


def test_cli_plan_deploy_status_and_rollback(settings, site_payload, monkeypatch):
    repository = Repository(settings.database_path)
    repository.create_site(CdnSite.model_validate(site_payload))
    fake = FakeRunner(
        [
            CommandResult(0, "plan", ""),
            CommandResult(0, "apply", ""),
            CommandResult(0, "rollback", ""),
        ]
    )
    control = ControlPlane(settings, repository, fake)  # type: ignore[arg-type]
    monkeypatch.setattr(cli, "_control_plane", lambda: control)
    planned = runner.invoke(cli.app, ["plan", "--json"])
    assert planned.exit_code == 0
    deployed = runner.invoke(cli.app, ["deploy", "--yes", "--json"])
    assert deployed.exit_code == 0
    deployment_id = json.loads(deployed.stdout)["id"]
    assert runner.invoke(cli.app, ["status", deployment_id, "--json"]).exit_code == 0
    assert runner.invoke(cli.app, ["status", "--json"]).exit_code == 0
    assert (
        runner.invoke(cli.app, ["rollback", deployment_id, "--yes", "--json"]).exit_code
        == 0
    )
