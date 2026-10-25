"""An edge, as Ansible's connection variables.

The inventory plugin publishes the same variables from the same rows; it runs
inside Ansible and cannot import this, so ``tests/contract/test_inventory.py``
keeps them in step.
"""

from __future__ import annotations

from typing import Any

from blitzecdn.capabilities.edges.domain import Edge

__all__ = ["edge_to_inventory"]


def edge_to_inventory(edge: Edge) -> dict[str, Any]:
    variables: dict[str, Any] = {
        "ansible_host": edge.host,
        "ansible_user": edge.user,
        "ansible_port": edge.port,
        "blitzecdn_firewall_ssh_port": edge.port,
    }
    if edge.private_key_file is not None:
        variables["ansible_ssh_private_key_file"] = edge.private_key_file
    if edge.public_addresses:
        variables["blitzecdn_public_addresses"] = list(edge.public_addresses)
    return variables
