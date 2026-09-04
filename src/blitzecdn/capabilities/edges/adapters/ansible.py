"""An edge, as Ansible's connection variables.

The mirror of ``blitzecdn.capabilities.sites.adapters.ansible``: the
capability that owns the model owns the projection of it. The ``blitzecdn``
inventory plugin publishes the same variables from the same rows and is kept in
step by ``tests/contract/test_inventory.py``, because that plugin runs inside
Ansible's own interpreter and cannot import this.
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
