"""`plan`, `deploy`, `rollback` and `drift`, and the exit codes they use."""

import json

from support.cli import (
    _control,
    runner,
)
from support.control_plane import (
    FakeRunner,
    ansible_run,
    build_application,
    host_run,
    seed_edge,
    seed_site,
)

from blitzecdn.capabilities.deployments import cli as deployments_cli
from blitzecdn.capabilities.dns.domain import Domain
from blitzecdn.composition import Repository
from blitzecdn.core.domain.runs import RunStatus
from blitzecdn.core.exceptions import DeploymentBusyError
from blitzecdn.entrypoints.cli import main as cli


def _fleet(settings, monkeypatch, runner_double=None):
    """A CLI-wired control plane with one edge in it.

    Per test rather than in the shared helper, since roster tests assert on an
    empty fleet.
    """
    control = _control(settings, monkeypatch, runner_double)
    seed_edge(control)
    return control


def test_cli_plan_deploy_status_and_rollback(settings, monkeypatch):
    repository = Repository(settings.database_path)
    fake = FakeRunner(
        [
            ansible_run(host_run("edge-a")),
            ansible_run(host_run("edge-a")),
            ansible_run(host_run("edge-a")),
        ]
    )
    control = build_application(settings=settings, repository=repository, runner=fake)
    seed_edge(control)
    seed_site(control)
    monkeypatch.setattr(cli.common, "container", lambda: control.container)
    planned = runner.invoke(cli.app, ["plan", "--json"])
    assert planned.exit_code == 0
    # --skip-preflight: one scripted run per command. The validate-and-preview flow
    # has its own test below.
    deployed = runner.invoke(cli.app, ["deploy", "--yes", "--skip-preflight", "--json"])
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
    control = build_application(settings=settings, repository=repository, runner=fake)
    seed_edge(control)
    # Cache and compression off: this is about the interactive flow, not about
    # which capabilities the site requests.
    seed_site(control, cache_enabled=False, compression="off")
    monkeypatch.setattr(cli.common, "container", lambda: control.container)
    result = runner.invoke(cli.app, ["deploy"], input="y\n")
    assert result.exit_code == 0
    assert "Configuration is valid" in result.stdout
    # The preview names the task that would change, not Ansible's raw output.
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
    _fleet(settings, monkeypatch, FakeRunner([_in_sync()]))
    result = runner.invoke(cli.app, ["drift"])
    assert result.exit_code == 0
    assert "All 1 edges match desired state." in result.output


def test_drift_exits_six_when_an_edge_has_moved(settings, monkeypatch):
    """A dedicated code so a scheduled check can tell drift from a broken check."""
    _fleet(settings, monkeypatch, FakeRunner([_drifted()]))
    result = runner.invoke(cli.app, ["drift"])
    assert result.exit_code == cli.ExitCode.DRIFT_DETECTED
    assert "edge-a would change 2 task(s)" in result.output


def test_drift_json_output_is_machine_readable(settings, monkeypatch):
    _fleet(settings, monkeypatch, FakeRunner([_drifted()]))
    result = runner.invoke(cli.app, ["drift", "--json"])
    payload = json.loads(result.output)
    assert payload["in_sync"] is False
    assert payload["hosts"][0]["changed"] == 2


def test_deploy_with_a_limit_says_the_rollout_is_unfinished(settings, monkeypatch):
    """Leaving a canary half-applied silently is the failure worth preventing."""
    control = _fleet(
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
    control = build_application(
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
    )
    seed_edge(control)
    monkeypatch.setattr(cli.common, "container", lambda: control.container)

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
    control = build_application(settings=settings, repository=repository, runner=fake)
    seed_edge(control)
    seed_site(control, cache_enabled=False, compression="off")
    monkeypatch.setattr(cli.common, "container", lambda: control.container)

    result = runner.invoke(cli.app, ["deploy"], input="n\n")

    assert result.exit_code == 1
    # The preview ran the edge's VALIDATE phase in check mode; declining meant
    # nothing ran with `check=False` or was staged.
    assert fake.check_modes == [True]
    assert fake.tag_selections == [()]


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
    _fleet(settings, monkeypatch, FakeRunner([ansible_run()]))

    result = runner.invoke(cli.app, ["drift"])

    assert result.exit_code == cli.ExitCode.DRIFT_DETECTED
    assert "No edge reported a result" in result.output


def test_yes_answers_the_prompt_without_giving_up_the_preflight(settings, monkeypatch):
    """--yes means "do not prompt". It does not mean "do not check".

    Validation and the check-mode preview still run; skipping them is
    --skip-preflight, spelled out.
    """
    fake = FakeRunner(
        [
            ansible_run(host_run("edge-a", changes=("Render managed sites",))),
            ansible_run(host_run("edge-a", changes=("Render managed sites",))),
        ]
    )
    control = build_application(
        settings=settings, repository=Repository(settings.database_path), runner=fake
    )
    seed_edge(control)
    seed_site(control, cache_enabled=False, compression="off")
    monkeypatch.setattr(cli.common, "container", lambda: control.container)

    # No `input=`: a prompt here would fail the test rather than hang it, which
    # is the point — --yes still answers the confirmation.
    result = runner.invoke(cli.app, ["deploy", "--yes"])

    assert result.exit_code == 0
    assert "Configuration is valid" in result.stdout
    assert "edge-a: would change 1 task(s)" in result.stdout


def test_skip_preflight_is_what_gives_up_the_checks(settings, monkeypatch):
    """The escape hatch still exists; it just has to be asked for by name."""
    fake = FakeRunner([ansible_run(host_run("edge-a"))])
    control = build_application(
        settings=settings, repository=Repository(settings.database_path), runner=fake
    )
    seed_site(control, cache_enabled=False, compression="off")
    monkeypatch.setattr(cli.common, "container", lambda: control.container)

    result = runner.invoke(cli.app, ["deploy", "--yes", "--skip-preflight"])

    assert result.exit_code == 0
    # One scripted run was enough, so no preview ran.
    assert "Configuration is valid" not in result.stdout


class _BusyRunner(FakeRunner):
    """A fleet whose deployment lock another run holds for the first ``busy`` tries."""

    def __init__(self, runs, *, busy):
        super().__init__(runs)
        self.busy = busy

    def lock(self):
        if self.busy:
            self.busy -= 1
            raise DeploymentBusyError("another deployment is already running")
        return super().lock()


def test_plan_fails_at_once_when_another_run_holds_the_lock(settings, monkeypatch):
    control = _fleet(
        settings, monkeypatch, _BusyRunner([ansible_run(host_run("edge-a"))], busy=1)
    )
    planned = runner.invoke(cli.app, ["plan", "--json"])
    # Raised to the entry point, which maps it to exit code BUSY.
    assert isinstance(planned.exception, DeploymentBusyError)
    assert control.repository.deployments.list_deployments() == []


def test_plan_and_deploy_wait_for_the_lock_when_asked(settings, monkeypatch):
    monkeypatch.setattr(deployments_cli, "_WAIT_POLL_SECONDS", 0.01)
    fake = _BusyRunner(
        [ansible_run(host_run("edge-a")), ansible_run(host_run("edge-a"))], busy=3
    )
    control = _fleet(settings, monkeypatch, fake)
    planned = runner.invoke(cli.app, ["plan", "--wait", "10", "--json"])
    assert planned.exit_code == 0, planned.output
    # Refused attempts took the lock before writing, so they left no record.
    assert len(control.repository.deployments.list_deployments()) == 1

    fake.busy = 3
    deployed = runner.invoke(
        cli.app, ["deploy", "--yes", "--skip-preflight", "--wait", "10", "--json"]
    )
    assert deployed.exit_code == 0, deployed.output
    assert len(control.repository.deployments.list_deployments()) == 2


def test_waiting_gives_up_after_the_bound(settings, monkeypatch):
    monkeypatch.setattr(deployments_cli, "_WAIT_POLL_SECONDS", 0.05)
    fake = _BusyRunner([ansible_run(host_run("edge-a"))], busy=10_000)
    _fleet(settings, monkeypatch, fake)
    drifted = runner.invoke(cli.app, ["drift", "--wait", "1", "--json"])
    # Raised to the entry point, which maps it to exit code BUSY.
    assert isinstance(drifted.exception, DeploymentBusyError)
