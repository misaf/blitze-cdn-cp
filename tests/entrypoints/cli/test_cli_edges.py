"""Registering, updating and decommissioning an edge from the command line."""

import json

from click.utils import strip_ansi
from support.cli import (
    cli_control,
    runner,
    store,
)
from support.control_plane import (
    FakeRunner,
    ansible_run,
    host_run,
)
from support.host_keys import FIRST, FIRST_FINGERPRINT, SECOND, SECOND_FINGERPRINT
from support.stores import Stores

from blitzecdn.capabilities.edges.adapters.host_keys import KnownHosts
from blitzecdn.core.domain.runs import RunStatus
from blitzecdn.entrypoints.cli import main as cli


def test_setup_and_edge_workflow(tmp_path, monkeypatch):
    """Register, list, update and remove an edge, with no inventory file.

    Ansible reads the fleet table through the `blitzecdn` inventory plugin, so the
    roster is the inventory.
    """
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli.app, ["setup"])
    assert result.exit_code == 0
    assert not (tmp_path / "ansible/inventory/hosts.yml").exists()
    # The inventory plugin refuses a missing database, so `setup` must create one.
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
    """Who added this host, and when."""
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

    events = Stores(settings.database_path).audit_log.list_audit_events()
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
    cli_control(settings, monkeypatch, double)
    _add_edge()

    result = runner.invoke(cli.app, ["edge", "remove", "edge-01", "--yes"])

    assert result.exit_code == 0
    assert double.decommissions == ["edge-01"]
    assert json.loads(runner.invoke(cli.app, ["edge", "list", "--json"]).stdout) == []


def test_edge_remove_keeps_the_entry_when_the_teardown_fails(settings, monkeypatch):
    """An unreachable host keeps its entry: its private keys are still on it."""
    double = FakeRunner([_unreachable()])
    cli_control(settings, monkeypatch, double)
    _add_edge()

    result = runner.invoke(cli.app, ["edge", "remove", "edge-01", "--yes"])

    assert result.exit_code != 0
    listed = json.loads(runner.invoke(cli.app, ["edge", "list", "--json"]).stdout)
    assert [edge["name"] for edge in listed] == ["edge-01"]


def test_edge_remove_force_drops_a_host_that_no_longer_exists(settings, monkeypatch):
    """--force is for a destroyed instance, which can never report clean."""
    double = FakeRunner([_unreachable()])
    cli_control(settings, monkeypatch, double)
    _add_edge()

    result = runner.invoke(cli.app, ["edge", "remove", "edge-01", "--yes", "--force"])

    assert result.exit_code == 0
    assert json.loads(runner.invoke(cli.app, ["edge", "list", "--json"]).stdout) == []
    actions = [event.action for event in store(settings).audit_log.list_audit_events()]
    assert "edge.decommission_failed" in actions


def test_edge_remove_keeps_the_edge_when_the_operator_declines(settings, monkeypatch):
    cli_control(settings, monkeypatch)
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
    cli_control(settings, monkeypatch)

    result = runner.invoke(
        cli.app,
        ["edge", "add", "edge-01", "--host", "192.0.2.10"],
        color=False,
    )

    assert result.exit_code != 0
    assert "ssh-source" in strip_ansi(result.output)


# --- SSH host keys -----------------------------------------------------------
#
# The controller connects with StrictHostKeyChecking and BatchMode, so an edge
# without a trusted key fails every deploy. A controller "manages" host keys
# once its state holds the SSH configuration the installer writes.


def _scanned(monkeypatch, *keys):
    """`ssh-keyscan` answering with ``keys``; returns the addresses scanned."""
    scans: list[tuple[str, int]] = []

    def scan(self, host, port):
        scans.append((host, port))
        return keys

    monkeypatch.setattr(KnownHosts, "scan", scan)
    return scans


def _managed(settings):
    ssh = settings.state_dir / ".ssh"
    ssh.mkdir(parents=True, exist_ok=True)
    (ssh / "config").write_text("", encoding="utf-8")
    return KnownHosts(settings)


def _listed(settings):
    return [edge.name for edge in store(settings).edges.list_edges()]


def test_edge_add_trusts_only_the_key_matching_the_given_fingerprint(
    settings, monkeypatch
):
    cli_control(settings, monkeypatch)
    known_hosts = _managed(settings)
    scans = _scanned(monkeypatch, FIRST, SECOND)

    result = runner.invoke(
        cli.app,
        [
            "edge",
            "add",
            "edge-01",
            "--host",
            "192.0.2.10",
            "--port",
            "7845",
            "--ssh-source",
            "198.51.100.8/24",
            "--host-key-fingerprint",
            SECOND_FINGERPRINT,
        ],
    )

    assert result.exit_code == 0, result.output
    assert scans == [("192.0.2.10", 7845)]
    assert known_hosts.trusted("192.0.2.10", 7845) == (SECOND,)
    trusted = next(
        event
        for event in store(settings).audit_log.list_audit_events()
        if event.action == "edge.host_key_trusted"
    )
    assert trusted.details["fingerprints"] == [f"ssh-ed25519 {SECOND_FINGERPRINT}"]


def test_edge_add_registers_nothing_when_the_fingerprint_does_not_match(
    settings, monkeypatch
):
    cli_control(settings, monkeypatch)
    known_hosts = _managed(settings)
    _scanned(monkeypatch, FIRST)

    result = runner.invoke(
        cli.app,
        [
            "edge",
            "add",
            "edge-01",
            "--host",
            "192.0.2.10",
            "--ssh-source",
            "198.51.100.8/24",
            "--host-key-fingerprint",
            SECOND_FINGERPRINT,
        ],
    )

    assert result.exit_code != 0
    assert _listed(settings) == []
    assert known_hosts.trusted("192.0.2.10", 22) == ()


def test_edge_add_shows_the_fingerprints_and_trusts_them_once_confirmed(
    settings, monkeypatch
):
    cli_control(settings, monkeypatch)
    known_hosts = _managed(settings)
    _scanned(monkeypatch, FIRST)

    result = runner.invoke(cli.app, _ADD, input="y\n")

    assert result.exit_code == 0, result.output
    assert FIRST_FINGERPRINT in result.output
    assert known_hosts.trusted("192.0.2.10", 22) == (FIRST,)
    assert _listed(settings) == ["edge-01"]


def test_edge_add_registers_nothing_when_the_fingerprint_is_declined(
    settings, monkeypatch
):
    """Declining, or no terminal to answer on, is never trust on first use."""
    cli_control(settings, monkeypatch)
    known_hosts = _managed(settings)
    _scanned(monkeypatch, FIRST)

    declined = runner.invoke(cli.app, _ADD, input="n\n")
    unanswered = runner.invoke(cli.app, _ADD, input="")

    assert declined.exit_code != 0
    assert unanswered.exit_code != 0
    assert known_hosts.trusted("192.0.2.10", 22) == ()
    assert _listed(settings) == []


def test_edge_add_can_defer_the_host_key_for_a_host_not_up_yet(settings, monkeypatch):
    cli_control(settings, monkeypatch)
    _managed(settings)
    scans = _scanned(monkeypatch, FIRST)

    result = runner.invoke(cli.app, [*_ADD, "--skip-host-key"])

    assert result.exit_code == 0, result.output
    assert scans == []
    assert "edge host-key edge-01" in result.output


def test_a_checkout_leaves_host_keys_to_the_operators_ssh(settings, monkeypatch):
    cli_control(settings, monkeypatch)
    scans = _scanned(monkeypatch, FIRST)

    assert runner.invoke(cli.app, _ADD).exit_code == 0
    assert scans == []


def test_edge_host_key_replaces_a_rotated_key(settings, monkeypatch):
    cli_control(settings, monkeypatch)
    known_hosts = _managed(settings)
    _scanned(monkeypatch, FIRST)
    assert runner.invoke(cli.app, _ADD, input="y\n").exit_code == 0
    _scanned(monkeypatch, SECOND)

    result = runner.invoke(
        cli.app,
        ["edge", "host-key", "edge-01", "--host-key-fingerprint", SECOND_FINGERPRINT],
    )

    assert result.exit_code == 0, result.output
    assert FIRST_FINGERPRINT in result.output  # what it replaces
    assert known_hosts.trusted("192.0.2.10", 22) == (SECOND,)


def test_edge_update_to_a_new_address_points_at_host_key_enrollment(
    settings, monkeypatch
):
    cli_control(settings, monkeypatch)
    _managed(settings)
    _scanned(monkeypatch, FIRST)
    assert runner.invoke(cli.app, _ADD, input="y\n").exit_code == 0

    result = runner.invoke(cli.app, ["edge", "update", "edge-01", "--port", "7845"])

    assert result.exit_code == 0, result.output
    assert "edge host-key edge-01" in result.output


_ADD = [
    "edge",
    "add",
    "edge-01",
    "--host",
    "192.0.2.10",
    "--ssh-source",
    "198.51.100.8/24",
]


def test_edge_add_refuses_a_taken_name_before_scanning(settings, monkeypatch):
    cli_control(settings, monkeypatch)
    _managed(settings)
    _scanned(monkeypatch, FIRST)
    assert runner.invoke(cli.app, _ADD, input="y\n").exit_code == 0
    scans = _scanned(monkeypatch, SECOND)

    result = runner.invoke(cli.app, _ADD, input="y\n")

    assert result.exit_code != 0
    assert scans == []
