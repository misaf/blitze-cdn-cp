"""Verify the Ansible inventory against the fleet the control plane records.

The fleet is one table. Ansible learns about it through the `blitzecdn`
inventory plugin, which reads that table directly at the start of every run —
so there is no file to keep in step, and nothing here writes one.

What is left to check is the seam the plugin sits on, and it is a real seam:
the plugin ships in `ansible/plugins/inventory/`, runs inside `ansible-playbook`
under whatever interpreter that is, and cannot import `blitzecdn` to validate
what it reads. Two independent pieces of code therefore know the shape of an
edge — `domain.edges.Edge.to_inventory` and the plugin's `_host_variables` —
and the only honest way to check they agree is to write a database with the
model and then run the real `ansible-inventory` against it. That is what
`test_the_plugin_publishes_what_the_model_renders` does.

"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from blitzecdn.domain.edges import EDGE_GROUP, Edge, firewall_sources
from blitzecdn.infrastructure.database import Repository

PROJECT_DIR = Path(__file__).resolve().parent.parent
ANSIBLE_DIR = PROJECT_DIR / "ansible"
INVENTORY_SOURCE = ANSIBLE_DIR / "inventory/blitzecdn.yml"


def _executable() -> str:
    """Find `ansible-inventory`, preferring the one beside this interpreter.

    `shutil.which` alone finds nothing when the tests run through
    `.venv/bin/python` without the virtualenv activated, which is how CI and
    most editors invoke them — and a skip there would quietly stop checking the
    one thing in this file that cannot be checked any other way.
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

    Subprocess rather than importing the plugin, because importing it would
    test a different thing: the plugin only matters as something Ansible loads,
    resolves options for, and merges group_vars on top of. Anything that skips
    that skips the parts most likely to break.
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
    """Strip Ansible's `AnsibleUnsafe` wrappers from a `--list` document.

    Every string an inventory plugin produces comes back tagged this way in
    JSON. It is a marker meaning "do not template this", which is correct and
    irrelevant to whether the value is right.
    """
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

    One carries every optional field an operator can set — a non-default port,
    an explicit key, a non-default user — because those are exactly what a real
    installation had in its hand-edited inventory. The other carries none of
    them and separate public addresses, so the defaults and the NAT case are
    both covered.
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
    repository = Repository(database)
    for edge in edges:
        repository.edges.create_edge(edge)
    return database, edges


def test_the_plugin_publishes_what_the_model_renders(fleet):
    """The seam: two implementations of "an edge, as Ansible sees it".

    `Edge.to_inventory` is what the control plane believes it is publishing;
    the plugin's `_host_variables` is what Ansible is actually given. They
    cannot share code — one imports pydantic, the other must import nothing —
    so this asserts they agree on real output rather than trusting that whoever
    edits one remembers the other.
    """
    database, edges = fleet

    document = _ansible_inventory(database)

    assert document[EDGE_GROUP]["hosts"] == ["edge-01", "edge-02"]
    for edge in edges:
        published = document["_meta"]["hostvars"][edge.name]
        for key, expected in edge.to_inventory().items():
            assert published[key] == expected, f"{edge.name}.{key}"


def test_an_absent_optional_field_is_absent_rather_than_null(fleet):
    """`ansible_ssh_private_key_file: null` is not the same as omitting it.

    Emitted as null, Ansible hands SSH an empty identity file instead of
    letting it resolve a key the usual way — an agent, `~/.ssh/config`, the
    default identities — and the connection fails for a reason that appears
    nowhere in the inventory an operator would go and read.
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


def test_management_networks_reach_every_edge_and_outrank_group_vars(fleet):
    """The union, as a host variable, which is the whole point of it being one.

    `inventory/group_vars/blitzecdn_edges/` is loaded for this group and
    outranks any *group* variable an inventory declares. Published per group,
    these CIDRs would lose to that file — which is what happened while the
    fleet lived in a static inventory, and why `edge add --ssh-source` recorded
    a value the firewall never applied. A host variable outranks both.
    """
    database, edges = fleet
    expected = firewall_sources(edges)
    assert expected == ["94.183.119.90/32", "203.0.113.0/24"]

    hostvars = _ansible_inventory(database)["_meta"]["hostvars"]

    for name in ("edge-01", "edge-02"):
        assert hostvars[name]["blitzecdn_firewall_ssh_sources"] == expected


def test_the_firewall_port_follows_the_port_ansible_connects_on(fleet):
    """One field, published twice, so the two can never disagree.

    A firewall opening a different port from the one Ansible connects on
    strands the host: the next converge closes the door behind itself and no
    later deploy can reach it to undo that. They used to be two settings in two
    files maintained by hand.
    """
    database, _ = fleet

    hostvars = _ansible_inventory(database)["_meta"]["hostvars"]

    assert hostvars["edge-01"]["blitzecdn_firewall_ssh_port"] == 7845
    assert hostvars["edge-02"]["blitzecdn_firewall_ssh_port"] == 22


def test_an_empty_fleet_still_declares_the_group(tmp_path: Path):
    """A play limited to the group must skip, not fail on an unknown pattern.

    Those read very differently at three in the morning: "no hosts matched" is
    a fleet with nothing in it, while an unknown pattern looks like the
    inventory itself is broken.
    """
    database = tmp_path / "control-plane.db"
    Repository(database)

    document = _ansible_inventory(database)

    # An empty group is serialised as membership of `all` with no `hosts` key,
    # rather than as an entry of its own — so this is what "the group exists"
    # looks like in `--list`, and it is enough for `--limit blitzecdn_edges` to
    # resolve to nothing instead of raising on an unknown pattern.
    assert EDGE_GROUP in document["all"]["children"]
    assert document.get(EDGE_GROUP, {}).get("hosts", []) == []


def test_a_schema_version_from_the_future_is_refused(fleet):
    """A stale Ansible tree must fail loudly, not publish half an edge.

    The plugin and the control plane are upgraded separately — one is a Python
    package, the other a directory of YAML someone may have deployed from a
    different checkout. A row written by a newer control plane is refused,
    because the alternative is converging a fleet from fields this plugin does
    not know it is missing.
    """
    database, _ = fleet
    import sqlite3

    connection = sqlite3.connect(database)
    connection.execute("UPDATE edges SET schema_version = 99 WHERE name = 'edge-01'")
    connection.commit()
    connection.close()

    completed = subprocess.run(  # noqa: S603 - fixed argv built in this test
        [_executable(), "-i", str(INVENTORY_SOURCE), "--list"],
        cwd=ANSIBLE_DIR,
        env={
            **os.environ,
            "ANSIBLE_CONFIG": str(ANSIBLE_DIR / "ansible.cfg"),
            "BLITZE_DATABASE_PATH": str(database),
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode != 0
    assert "schema version 99" in completed.stderr
