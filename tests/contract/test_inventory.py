"""Verify the Ansible inventory against the fleet the control plane records.

The `blitzecdn` inventory plugin reads the `edges` table at the start of every
run. It runs inside `ansible-playbook` and cannot import `blitzecdn`, so two
pieces of code know an edge's shape:
`capabilities.edges.adapters.ansible.edge_to_inventory` and the plugin's
`_host_variables`. These tests write a database with the model and run the
real `ansible-inventory` against it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from support.paths import CORE_ANSIBLE, REPO_ROOT

from blitzecdn.capabilities.edges.adapters.ansible import edge_to_inventory
from blitzecdn.capabilities.edges.domain import EDGE_GROUP, Edge, firewall_sources
from blitzecdn.composition import Repository
from blitzecdn.core.domain.validation import RESERVED_ANSIBLE_SETTINGS
from blitzecdn.core.persistence.schema import DatabaseSchema

PROJECT_DIR = REPO_ROOT
ANSIBLE_DIR = CORE_ANSIBLE
INVENTORY_SOURCE = ANSIBLE_DIR / "inventory/blitzecdn.yml"


def _executable() -> str:
    """Find `ansible-inventory`, preferring the one beside this interpreter.

    `shutil.which` finds nothing when tests run through `.venv/bin/python`
    without the virtualenv activated.
    """
    local = Path(sys.executable).parent / "ansible-inventory"
    if local.is_file():
        return str(local)
    found = shutil.which("ansible-inventory")
    if found is None:
        pytest.skip("ansible-inventory is not installed")
    return found


def _ansible_inventory(database: Path) -> dict:
    """Run the real `ansible-inventory` against a database, as a deploy would.

    A subprocess, because the plugin matters as something Ansible loads, resolves
    options for and merges group_vars on top of.
    """
    environment = {
        **os.environ,
        "ANSIBLE_CONFIG": str(ANSIBLE_DIR / "ansible.cfg"),
        "BLITZE_DATABASE_PATH": str(database),
    }
    completed = subprocess.run(  # noqa: S603 - fixed argv built in this test
        [_executable(), "-i", str(INVENTORY_SOURCE), "--list"],
        cwd=ANSIBLE_DIR,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return _plain(json.loads(completed.stdout))


def _plain(value):
    """Strip Ansible's `AnsibleUnsafe` wrappers from a `--list` document."""
    if isinstance(value, dict):
        if set(value) == {"__ansible_unsafe"}:
            return value["__ansible_unsafe"]
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


@pytest.fixture
def fleet(tmp_path: Path) -> tuple[Path, list[Edge]]:
    """A database holding two deliberately dissimilar edges.

    One sets every optional field (port, key, user); the other none, with
    separate public addresses, so defaults and NAT are both covered.
    """
    database = tmp_path / "control-plane.db"
    edges = [
        Edge(
            name="edge-01",
            host="edge-01.mgmt.example.net",
            user="chakavak",
            port=7845,
            private_key_file="~/.ssh/blitzecdn",
            ssh_sources=("94.183.119.90/32",),
        ),
        Edge(
            name="edge-02",
            host="192.0.2.10",
            public_addresses=("198.51.100.5", "198.51.100.6"),
            ssh_sources=("203.0.113.0/24",),
        ),
    ]
    DatabaseSchema.migrate(database)
    repository = Repository(database)
    for edge in edges:
        repository.edges.create_edge(edge)
    return database, edges


def test_the_plugin_publishes_what_the_model_renders(fleet):
    """The seam: two implementations of "an edge, as Ansible sees it".

    They cannot share code, so this asserts they agree on real output.
    """
    database, edges = fleet

    document = _ansible_inventory(database)

    assert document[EDGE_GROUP]["hosts"] == ["edge-01", "edge-02"]
    for edge in edges:
        published = document["_meta"]["hostvars"][edge.name]
        for key, expected in edge_to_inventory(edge).items():
            assert published[key] == expected, f"{edge.name}.{key}"


def test_an_absent_optional_field_is_absent_rather_than_null(fleet):
    """`ansible_ssh_private_key_file: null` is not the same as omitting it.

    As null, Ansible hands SSH an empty identity file instead of resolving a key
    the usual way.
    """
    database, _ = fleet

    hostvars = _ansible_inventory(database)["_meta"]["hostvars"]

    assert "ansible_ssh_private_key_file" not in hostvars["edge-02"]
    assert "blitzecdn_public_addresses" not in hostvars["edge-01"]


def test_database_settings_override_shipped_group_vars(fleet):
    database, _ = fleet
    Repository(database).ansible_settings.set_setting(
        "blitzecdn_firewall_enabled", True
    )

    hostvars = _ansible_inventory(database)["_meta"]["hostvars"]

    assert hostvars["edge-01"]["blitzecdn_firewall_enabled"] is True
    assert hostvars["edge-02"]["blitzecdn_firewall_enabled"] is True


def test_the_plugin_refuses_to_publish_a_reserved_setting(fleet):
    """The last line of defence on a variable that can strand the whole fleet.

    The store refuses these names, so the row is inserted by hand, as a restored
    or hand-edited database would carry it. At host precedence it would beat the
    derived SSH port and the firewall would close the port the next converge
    needs.
    """
    database, _ = fleet
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO ansible_settings (name, value, updated_at) VALUES (?, ?, ?)",
        ("blitzecdn_firewall_ssh_port", "2222", "2026-01-01T00:00:00+00:00"),
    )
    connection.commit()
    connection.close()

    hostvars = _ansible_inventory(database)["_meta"]["hostvars"]

    assert hostvars["edge-01"]["blitzecdn_firewall_ssh_port"] == 7845
    assert hostvars["edge-02"]["blitzecdn_firewall_ssh_port"] == 22


def test_the_plugins_reserved_names_match_the_domains():
    """Two copies of one list, in files that cannot import each other.

    The plugin is loaded by path, from where Ansible loads it, so a name added on
    one side only fails here.
    """
    pytest.importorskip("ansible.plugins.inventory")
    plugin = ANSIBLE_DIR / "plugins/inventory/blitzecdn.py"
    spec = importlib.util.spec_from_file_location("blitzecdn_inventory", plugin)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.RESERVED_SETTINGS == RESERVED_ANSIBLE_SETTINGS
    assert module.EDGE_GROUP == EDGE_GROUP


def _plugin_module():
    pytest.importorskip("ansible.plugins.inventory")
    plugin = ANSIBLE_DIR / "plugins/inventory/blitzecdn.py"
    spec = importlib.util.spec_from_file_location("blitzecdn_inventory", plugin)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_default_database_is_the_checkouts_own():
    """A hand-run `ansible-inventory` in a checkout finds the controller's file."""
    source = ANSIBLE_DIR / "inventory/blitzecdn.yml"

    default = _plugin_module()._default_database(str(source))

    assert Path(default) == REPO_ROOT / ".state/control-plane.db"


def test_the_default_database_follows_the_nearest_project_marker(tmp_path, monkeypatch):
    module = _plugin_module()
    project = tmp_path / "project"
    inventory = project / "src/blitzecdn/assets/ansible/inventory"
    inventory.mkdir(parents=True)
    (project / "blitzecdn.toml").write_text("[blitzecdn]\n", encoding="utf-8")
    source = str(inventory / "blitzecdn.yml")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert Path(module._default_database(source)) == project / ".state/control-plane.db"

    (project / "blitzecdn.toml").unlink()

    assert Path(module._default_database(source)) == (
        elsewhere / ".state/control-plane.db"
    )


def test_management_networks_reach_every_edge_and_outrank_group_vars(fleet):
    """The union, as a host variable, so it outranks group variables.

    `inventory/group_vars/blitzecdn_edges/` outranks any group variable an
    inventory declares; a host variable outranks both.
    """
    database, edges = fleet
    expected = firewall_sources(edges)
    assert expected == ["94.183.119.90/32", "203.0.113.0/24"]

    hostvars = _ansible_inventory(database)["_meta"]["hostvars"]

    for name in ("edge-01", "edge-02"):
        assert hostvars[name]["blitzecdn_firewall_ssh_sources"] == expected


def test_the_firewall_port_follows_the_port_ansible_connects_on(fleet):
    """One field, published twice, so the two can never disagree.

    A firewall opening a different port from the one Ansible connects on strands
    the host.
    """
    database, _ = fleet

    hostvars = _ansible_inventory(database)["_meta"]["hostvars"]

    assert hostvars["edge-01"]["blitzecdn_firewall_ssh_port"] == 7845
    assert hostvars["edge-02"]["blitzecdn_firewall_ssh_port"] == 22


def test_an_empty_fleet_still_declares_the_group(tmp_path: Path):
    """A play limited to the group must skip, not fail on an unknown pattern.

    "No hosts matched" says the fleet is empty; an unknown pattern looks like a
    broken inventory.
    """
    database = tmp_path / "control-plane.db"
    DatabaseSchema.migrate(database)

    document = _ansible_inventory(database)

    # An empty group is serialised as membership of `all` with no `hosts` key,
    # which is enough for `--limit blitzecdn_edges` to resolve to nothing.
    assert EDGE_GROUP in document["all"]["children"]
    assert document.get(EDGE_GROUP, {}).get("hosts", []) == []
