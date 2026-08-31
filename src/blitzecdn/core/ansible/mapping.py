"""Translate domain values into the documents consumed by Ansible."""

from __future__ import annotations

from typing import Any

from blitzecdn.features.edges.domain import Edge
from blitzecdn.features.sites.domain import CdnSite


def site_to_ansible(site: CdnSite) -> dict[str, Any]:
    document = site.model_dump(mode="json", exclude_none=True)
    if site.firewall.empty:
        del document["firewall"]
    return document


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
