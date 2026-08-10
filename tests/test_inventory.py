from pathlib import Path

import pytest

from blitzecdn.exceptions import ConfigurationError, ConflictError
from blitzecdn.infrastructure.inventory import Inventory


def test_inventory_manages_edges_and_normalizes_management_networks(tmp_path: Path):
    inventory = Inventory(tmp_path / "inventory/hosts.yml")
    assert inventory.initialize()
    assert not inventory.initialize()
    created = inventory.add_edge(
        "edge-01",
        host="192.0.2.10",
        user="deploy",
        ssh_sources=["198.51.100.8/24"],
        public_addresses=["203.0.113.10", "edge.example.net"],
    )
    assert created["name"] == "edge-01"
    assert inventory.list_edges() == [
        {
            "name": "edge-01",
            "host": "192.0.2.10",
            "user": "deploy",
            "public_addresses": ["203.0.113.10", "edge.example.net"],
        }
    ]
    updated = inventory.set_public_addresses("edge-01", ["203.0.113.11"])
    assert updated["public_addresses"] == ["203.0.113.11"]
    with pytest.raises(ConflictError):
        inventory.add_edge(
            "edge-01", host="other", user="deploy", ssh_sources=["192.0.2.0/24"]
        )
    inventory.remove_edge("edge-01")
    assert inventory.list_edges() == []


def test_inventory_rejects_invalid_management_network(tmp_path: Path):
    inventory = Inventory(tmp_path / "hosts.yml")
    with pytest.raises(ConfigurationError, match="invalid management CIDR"):
        inventory.add_edge(
            "edge-01", host="edge.example.com", user="deploy", ssh_sources=["anywhere"]
        )


def test_inventory_rejects_invalid_public_address(tmp_path: Path):
    inventory = Inventory(tmp_path / "hosts.yml")
    with pytest.raises(ConfigurationError, match="public edge addresses"):
        inventory.add_edge(
            "edge-01",
            host="localhost",
            user="deploy",
            ssh_sources=["192.0.2.1/32"],
            public_addresses=["not an address"],
        )


def test_inventory_requires_an_existing_edge_for_public_address_updates(tmp_path: Path):
    inventory = Inventory(tmp_path / "hosts.yml")
    inventory.initialize()
    with pytest.raises(ConfigurationError, match="edge does not exist"):
        inventory.set_public_addresses("missing", ["203.0.113.10"])


def test_group_vars_overrides_file_is_created_and_never_overwritten(tmp_path):
    """Site configuration needs a home the installer will not fight over.

    `defaults.yml` is tracked and replaced on upgrade, so editing it makes
    `./install.sh update` refuse with "tracked files have local changes". This
    file is gitignored and loaded after it, and must survive re-running setup —
    overwriting it would discard the operator's configuration on every upgrade.
    """
    directory = tmp_path / "inventory/group_vars/blitzecdn_edges"
    directory.mkdir(parents=True)
    inventory = Inventory(tmp_path / "inventory/hosts.yml")

    created = inventory.initialize_group_vars()

    assert created == directory / "local.yml"
    created.write_text("---\nblitzecdn_firewall_ssh_port: 7845\n", encoding="utf-8")

    assert inventory.initialize_group_vars() is None
    assert "7845" in created.read_text(encoding="utf-8")


def test_group_vars_overrides_are_skipped_without_the_directory(tmp_path):
    """An install predating the directory layout must not gain a stray file."""
    inventory = Inventory(tmp_path / "inventory/hosts.yml")
    (tmp_path / "inventory").mkdir()

    assert inventory.initialize_group_vars() is None
