"""`plan`, `deploy`, `rollback` and `drift`, and the exit codes they use."""

import json

from cli_support import (
    _control,
    runner,
)
from control_plane_fixtures import (
    FakeRunner,
    ansible_run,
    host_run,
    seed_site,
)

from blitzecdn.capabilities.dns.domain import Domain
from blitzecdn.cli import main as cli
from blitzecdn.composition import ControlPlane, Repository
from blitzecdn.core.domain.runs import RunStatus


def test_cli_plan_deploy_status_and_rollback(settings, monkeypatch):
    repository = Repository(settings.database_path)
    fake = FakeRunner(
        [
            ansible_run(host_run("edge-a")),
            ansible_run(host_run("edge-a")),
            ansible_run(host_run("edge-a")),
        ]
    )
    control = ControlPlane(settings=settings, repository=repository, runner=fake)  # type: ignore[arg-type]
    seed_site(control)
    monkeypatch.setattr(cli.common, "control_plane", lambda: control)
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


def test_interactive_deploy_validates_previews_and_applies(settings, monkeypatch):
    repository = Repository(settings.database_path)
    # `validate` reads the first result without consuming it, so one entry
    # serves both it and the preview; the second is the apply.
    fake = FakeRunner(
        [
            ansible_run(host_run("edge-a", changes=("Render managed sites",))),
            ansible_run(host_run("edge-a", changes=("Render managed sites",))),
        ]
    )
    control = ControlPlane(settings=settings, repository=repository, runner=fake)  # type: ignore[arg-type]
    # Nothing optional: this is about the interactive flow, and a site left on
    # its defaults asks for `cache` and `compression`, which the core-only
    # workspace does not have and would refuse the deploy over.
    seed_site(control, cache_enabled=False, compression="off")
    monkeypatch.setattr(cli.common, "control_plane", lambda: control)
    result = runner.invoke(cli.app, ["deploy"], input="y\n")
    assert result.exit_code == 0
    assert "Configuration is valid" in result.stdout
    # The preview names the task that would change. It used to echo Ansible's
    # own output verbatim, which meant several hundred lines to find this in.
    assert "edge-a: would change 1 task(s)" in result.stdout
    assert "Render managed sites" in result.stdout
    assert "succeeded" in result.stdout


def _in_sync():
    return ansible_run(host_run("edge-a", ok=9))


def _drifted():
    return ansible_run(
        host_run("edge-a", ok=9, changes=("Render managed sites", "Reload Nginx"))
    )


def test_drift_exits_zero_when_the_fleet_matches(settings, monkeypatch):
    _control(settings, monkeypatch, FakeRunner([_in_sync()]))
    result = runner.invoke(cli.app, ["drift"])
    assert result.exit_code == 0
    assert "All 1 edges match desired state." in result.output


def test_drift_exits_six_when_an_edge_has_moved(settings, monkeypatch):
    """A dedicated code so a scheduled check can tell drift from a broken check."""
    _control(settings, monkeypatch, FakeRunner([_drifted()]))
    result = runner.invoke(cli.app, ["drift"])
    assert result.exit_code == cli.ExitCode.DRIFT_DETECTED
    assert "edge-a would change 2 task(s)" in result.output


def test_drift_json_output_is_machine_readable(settings, monkeypatch):
    _control(settings, monkeypatch, FakeRunner([_drifted()]))
    result = runner.invoke(cli.app, ["drift", "--json"])
    payload = json.loads(result.output)
    assert payload["in_sync"] is False
    assert payload["hosts"][0]["changed"] == 2


def test_deploy_with_a_limit_says_the_rollout_is_unfinished(settings, monkeypatch):
    """Leaving a canary half-applied silently is the failure worth preventing."""
    control = _control(
        settings,
        monkeypatch,
        FakeRunner([ansible_run(host_run("edge-a")) for _ in range(3)]),
    )
    settings.inventory_path.write_text(
        "all:\n  children:\n    blitzecdn_edges:\n      hosts:\n"
        "        edge-a:\n          ansible_host: 198.51.100.1\n",
        encoding="utf-8",
    )
    control.dns.create_domain(Domain(name="example.com"), "cli")

    result = runner.invoke(cli.app, ["deploy", "--yes", "--limit", "edge-a"])

    assert result.exit_code == 0
    assert "This was a canary against 'edge-a'" in result.output
    assert "without --limit to finish the rollout" in result.output


def test_validate_exits_three_and_lists_what_is_wrong(settings, monkeypatch):
    control = _control(settings, monkeypatch)
    monkeypatch.setattr(
        control.deployments, "validate", lambda: ["playbook is missing"]
    )

    result = runner.invoke(cli.app, ["validate"])

    assert result.exit_code == cli.ExitCode.CONFIGURATION
    assert "playbook is missing" in result.output


def test_plan_exits_five_when_check_mode_fails(settings, monkeypatch):
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner(
            [
                ansible_run(
                    host_run("edge-a", ok=0, unreachable=1),
                    status=RunStatus.FAILED,
                    return_code=2,
                )
            ]
        ),
    )  # type: ignore[arg-type]
    monkeypatch.setattr(cli.common, "control_plane", lambda: control)

    assert runner.invoke(cli.app, ["plan"]).exit_code == cli.ExitCode.DEPLOYMENT_FAILED


def test_interactive_deploy_refuses_to_preview_an_invalid_configuration(
    settings, monkeypatch
):
    """The preview costs an Ansible run, so validation gates it."""
    control = _control(settings, monkeypatch)
    monkeypatch.setattr(
        control.deployments, "validate", lambda: ["inventory has no edges"]
    )

    result = runner.invoke(cli.app, ["deploy"], input="y\n")

    assert result.exit_code == cli.ExitCode.CONFIGURATION
    assert "inventory has no edges" in result.output
    assert "Previewing changes" not in result.output


def test_interactive_deploy_applies_nothing_when_the_operator_declines(
    settings, monkeypatch
):
    repository = Repository(settings.database_path)
    fake = FakeRunner([ansible_run(host_run("edge-a")) for _ in range(2)])
    control = ControlPlane(settings=settings, repository=repository, runner=fake)  # type: ignore[arg-type]
    seed_site(control, cache_enabled=False, compression="off")
    monkeypatch.setattr(cli.common, "control_plane", lambda: control)

    result = runner.invoke(cli.app, ["deploy"], input="n\n")

    assert result.exit_code == 1
    # The preview ran in check mode; the apply never did.
    assert fake.check_modes == [True]


def test_rollback_changes_nothing_when_the_operator_declines(settings, monkeypatch):
    control = _control(settings, monkeypatch)
    called = []
    monkeypatch.setattr(
        control.deployments, "rollback", lambda *a, **k: called.append(a)
    )

    result = runner.invoke(cli.app, ["rollback", "some-deployment-id"], input="n\n")

    assert result.exit_code == 1
    assert called == []


def test_drift_says_so_when_no_edge_answered(settings, monkeypatch):
    """An empty recap is silence, not agreement, so it must not read as in-sync."""
    _control(settings, monkeypatch, FakeRunner([ansible_run()]))

    result = runner.invoke(cli.app, ["drift"])

    assert result.exit_code == cli.ExitCode.DRIFT_DETECTED
    assert "No edge reported a result" in result.output
