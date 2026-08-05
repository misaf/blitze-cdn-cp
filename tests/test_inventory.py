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
    )
    assert created["name"] == "edge-01"
    assert inventory.list_edges() == [
        {"name": "edge-01", "host": "192.0.2.10", "user": "deploy"}
    ]
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
