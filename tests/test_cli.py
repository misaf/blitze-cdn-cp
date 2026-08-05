import json
import sys

import pytest
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


def test_cli_domain_record_status_audit_and_doctor(settings, monkeypatch, tmp_path):
    control = ControlPlane(settings, Repository(settings.database_path), FakeRunner())  # type: ignore[arg-type]
    monkeypatch.setattr(cli, "_control_plane", lambda: control)
    monkeypatch.setattr(cli, "_settings", lambda: settings)
    assert runner.invoke(cli.app, ["domain", "add", "example.com"]).exit_code == 0
    assert (
        runner.invoke(
            cli.app,
            [
                "record",
                "add",
                "example.com",
                "cdn",
                "--value",
                "198.51.100.10",
                "--proxied",
            ],
        ).exit_code
        == 0
    )
    listed = runner.invoke(cli.app, ["site", "list", "--json"])
    assert listed.exit_code == 0 and "cdn-example-com" in listed.stdout
    assert runner.invoke(cli.app, ["doctor", "--json"]).exit_code == 0
    assert runner.invoke(cli.app, ["audit", "--json"]).exit_code == 0
    assert (
        runner.invoke(
            cli.app, ["record", "remove", "example.com", "cdn", "--yes"]
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(cli.app, ["domain", "remove", "example.com", "--yes"]).exit_code
        == 0
    )


def test_setup_and_edge_workflow(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli.app, ["setup"])
    assert result.exit_code == 0
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600
    inventory = tmp_path / "ansible/inventory/hosts.yml"
    assert inventory.exists()
    settings = cli.Settings.from_environment({}, project_dir=tmp_path)
    monkeypatch.setattr(cli, "_settings", lambda: settings)
    added = runner.invoke(
        cli.app,
        [
            "edge",
            "add",
            "edge-01",
            "--host",
            "192.0.2.10",
            "--ssh-source",
            "198.51.100.8/24",
            "--json",
        ],
    )
    assert added.exit_code == 0
    assert json.loads(added.stdout)["name"] == "edge-01"
    listed = runner.invoke(cli.app, ["edge", "list", "--json"])
    assert json.loads(listed.stdout)[0]["host"] == "192.0.2.10"
    assert runner.invoke(cli.app, ["edge", "remove", "edge-01", "--yes"]).exit_code == 0


def test_run_reports_domain_errors_without_a_traceback(settings, monkeypatch, capsys):
    """`run()` is outside Click, so it must exit rather than raise."""
    control = ControlPlane(settings, Repository(settings.database_path), FakeRunner())  # type: ignore[arg-type]
    monkeypatch.setattr(cli, "_control_plane", lambda: control)
    monkeypatch.setattr(cli, "_settings", lambda: settings)
    monkeypatch.setattr(sys, "argv", ["blitzecdn", "record", "list", "absent.example"])

    with pytest.raises(SystemExit) as exit_info:
        cli.run()

    assert exit_info.value.code == cli.ExitCode.INVALID_INPUT
    assert "does not exist" in capsys.readouterr().err


def test_cli_proxy_toggle_drives_the_derived_site(settings, monkeypatch):
    """`record proxy --on/--off` is the CDN switch for one subdomain."""
    control = ControlPlane(settings, Repository(settings.database_path), FakeRunner())  # type: ignore[arg-type]
    monkeypatch.setattr(cli, "_control_plane", lambda: control)
    runner.invoke(cli.app, ["domain", "add", "example.com"])
    added = runner.invoke(
        cli.app,
        ["record", "add", "example.com", "api", "--value", "198.51.100.20", "--json"],
    )
    assert added.exit_code == 0
    assert json.loads(added.stdout)["proxied"] is False
    # Unproxied, the edge knows nothing about it.
    assert json.loads(runner.invoke(cli.app, ["site", "list", "--json"]).stdout) == []

    toggled = runner.invoke(
        cli.app, ["record", "proxy", "example.com", "api", "--on", "--json"]
    )
    assert toggled.exit_code == 0
    sites = json.loads(runner.invoke(cli.app, ["site", "list", "--json"]).stdout)
    assert [site["server_names"] for site in sites] == [["api.example.com"]]
    assert sites[0]["origin_host"] == "198.51.100.20"

    runner.invoke(cli.app, ["record", "proxy", "example.com", "api", "--off"])
    assert json.loads(runner.invoke(cli.app, ["site", "list", "--json"]).stdout) == []


def test_cli_dns_export_hides_addresses_for_proxied_records(settings, monkeypatch):
    control = ControlPlane(settings, Repository(settings.database_path), FakeRunner())  # type: ignore[arg-type]
    monkeypatch.setattr(cli, "_control_plane", lambda: control)
    runner.invoke(cli.app, ["domain", "add", "example.com"])
    runner.invoke(
        cli.app,
        [
            "record",
            "add",
            "example.com",
            "api",
            "--value",
            "198.51.100.20",
            "--proxied",
        ],
    )
    exported = json.loads(runner.invoke(cli.app, ["dns", "export", "--json"]).stdout)
    assert exported[0]["proxied"] is True
    assert "value" not in exported[0]


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


def test_interactive_deploy_validates_previews_and_applies(
    settings, site_payload, monkeypatch
):
    repository = Repository(settings.database_path)
    repository.create_site(CdnSite.model_validate(site_payload))
    fake = FakeRunner(
        [
            CommandResult(0, "syntax ok", ""),
            CommandResult(0, "preview", ""),
            CommandResult(0, "applied", ""),
        ]
    )
    control = ControlPlane(settings, repository, fake)  # type: ignore[arg-type]
    monkeypatch.setattr(cli, "_control_plane", lambda: control)
    result = runner.invoke(cli.app, ["deploy"], input="y\n")
    assert result.exit_code == 0
    assert "Configuration is valid" in result.stdout
    assert "preview" in result.stdout
    assert "succeeded" in result.stdout
