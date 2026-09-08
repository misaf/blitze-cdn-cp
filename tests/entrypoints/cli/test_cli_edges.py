"""Registering, updating and decommissioning an edge from the command line."""

import json

from cli_support import (
    _control,
    _store,
    runner,
)
from click.utils import strip_ansi
from control_plane_fixtures import (
    FakeRunner,
    ansible_run,
    host_run,
)

from blitzecdn.cli import main as cli
from blitzecdn.composition import Repository
from blitzecdn.core.domain.runs import RunStatus


def test_setup_and_edge_workflow(tmp_path, monkeypatch):
    """Register, list, update and remove an edge — with no inventory file.

    `setup` no longer creates one. The fleet is a table, and Ansible reads it
    through the `blitzecdn` inventory plugin, so the roster these commands
    change *is* the inventory rather than something that has to be written out
    to become one.
    """
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli.app, ["setup"])
    assert result.exit_code == 0
    assert not (tmp_path / "ansible/inventory/hosts.yml").exists()
    # The inventory plugin refuses a database that does not exist, so a fresh
    # install where `setup` left none could not run a single playbook — every
    # one of them failed to parse its inventory before reaching a task.
    assert cli.Settings.from_environment(
        {}, project_dir=tmp_path
    ).database_path.exists()
    settings = cli.Settings.from_environment({}, project_dir=tmp_path)
    monkeypatch.setattr(cli.common, "settings", lambda: settings)
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
            "--public-address",
            "203.0.113.10",
            "--json",
        ],
    )
    assert added.exit_code == 0
    assert json.loads(added.stdout)["name"] == "edge-01"
    listed = runner.invoke(cli.app, ["edge", "list", "--json"])
    assert json.loads(listed.stdout)[0]["host"] == "192.0.2.10"
    updated = runner.invoke(
        cli.app,
        [
            "edge",
            "update",
            "edge-01",
            "--public-address",
            "203.0.113.11",
            "--json",
        ],
    )
    assert json.loads(updated.stdout)["public_addresses"] == ["203.0.113.11"]
    # --no-decommission because `setup` scaffolds no playbooks: this test is
    # about the roster, and the teardown path is covered below.
    removed = runner.invoke(
        cli.app, ["edge", "remove", "edge-01", "--yes", "--no-decommission"]
    )
    assert removed.exit_code == 0
    assert json.loads(runner.invoke(cli.app, ["edge", "list", "--json"]).stdout) == []


def test_registering_an_edge_is_audited(tmp_path, monkeypatch):
    """Who added this host, and when.

    Unanswerable before: the CLI held the inventory file and rewrote it
    directly, so nothing about the fleet ever reached the audit trail.
    """
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(cli.app, ["setup"]).exit_code == 0
    settings = cli.Settings.from_environment({}, project_dir=tmp_path)
    monkeypatch.setattr(cli.common, "settings", lambda: settings)

    runner.invoke(
        cli.app,
        [
            "edge",
            "add",
            "edge-01",
            "--host",
            "192.0.2.10",
            "--ssh-source",
            "10.0.0.0/8",
        ],
    )
    runner.invoke(cli.app, ["edge", "update", "edge-01", "--port", "7845"])

    events = Repository(settings.database_path).audit_log.list_audit_events()
    actions = [event.action for event in events]
    assert "edge.added" in actions
    assert "edge.updated" in actions
    update = next(event for event in events if event.action == "edge.updated")
    assert update.details["fields"] == ["port"]


def _torn_down():
    return ansible_run(host_run("edge-01", ok=12, changed=7))


def _unreachable():
    return ansible_run(
        host_run("edge-01", ok=0, unreachable=1),
        status=RunStatus.FAILED,
        return_code=4,
    )


def _add_edge():
    return runner.invoke(
        cli.app,
        [
            "edge",
            "add",
            "edge-01",
            "--host",
            "192.0.2.10",
            "--ssh-source",
            "198.51.100.8/24",
        ],
    )


def test_edge_remove_tears_the_host_down_before_forgetting_it(settings, monkeypatch):
    """The teardown has to reach the host while it is still in inventory."""
    double = FakeRunner([_torn_down()])
    _control(settings, monkeypatch, double)
    _add_edge()

    result = runner.invoke(cli.app, ["edge", "remove", "edge-01", "--yes"])

    assert result.exit_code == 0
    assert double.decommissions == ["edge-01"]
    assert json.loads(runner.invoke(cli.app, ["edge", "list", "--json"]).stdout) == []


def test_edge_remove_keeps_the_entry_when_the_teardown_fails(settings, monkeypatch):
    """An unreachable host keeps its entry: its private keys are still on it."""
    double = FakeRunner([_unreachable()])
    _control(settings, monkeypatch, double)
    _add_edge()

    result = runner.invoke(cli.app, ["edge", "remove", "edge-01", "--yes"])

    assert result.exit_code != 0
    listed = json.loads(runner.invoke(cli.app, ["edge", "list", "--json"]).stdout)
    assert [edge["name"] for edge in listed] == ["edge-01"]


def test_edge_remove_force_drops_a_host_that_no_longer_exists(settings, monkeypatch):
    """--force is for a destroyed instance, which can never report clean."""
    double = FakeRunner([_unreachable()])
    _control(settings, monkeypatch, double)
    _add_edge()

    result = runner.invoke(cli.app, ["edge", "remove", "edge-01", "--yes", "--force"])

    assert result.exit_code == 0
    assert json.loads(runner.invoke(cli.app, ["edge", "list", "--json"]).stdout) == []
    actions = [event.action for event in _store(settings).audit_log.list_audit_events()]
    assert "edge.decommission_failed" in actions


def test_edge_remove_keeps_the_edge_when_the_operator_declines(settings, monkeypatch):
    _control(settings, monkeypatch)
    runner.invoke(
        cli.app,
        [
            "edge",
            "add",
            "edge-01",
            "--host",
            "192.0.2.10",
            "--ssh-source",
            "198.51.100.8/24",
        ],
    )

    result = runner.invoke(cli.app, ["edge", "remove", "edge-01"], input="n\n")

    assert result.exit_code == 1
    assert json.loads(runner.invoke(cli.app, ["edge", "list", "--json"]).stdout)


def test_edge_add_refuses_to_open_ssh_to_the_world(settings, monkeypatch):
    """The firewall fails closed; an edge with no management CIDR cannot exist."""
    _control(settings, monkeypatch)

    result = runner.invoke(
        cli.app,
        ["edge", "add", "edge-01", "--host", "192.0.2.10"],
        color=False,
    )

    assert result.exit_code != 0
    assert "ssh-source" in strip_ansi(result.output)
