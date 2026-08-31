"""Translate domain values into the documents consumed by Ansible."""

from __future__ import annotations

from typing import Any

from blitzecdn.features.cache.domain import PurgeEntry
from blitzecdn.features.dns.site_domain import CdnSite
from blitzecdn.features.edges.domain import Edge


def site_to_ansible(site: CdnSite) -> dict[str, Any]:
    document = site.model_dump(mode="json", exclude_none=True)
    if site.firewall.empty:
        del document["firewall"]
    return document


def purge_entry_to_ansible(entry: PurgeEntry) -> dict[str, str]:
    return {"host": entry.host, "uri": entry.uri, "scheme": entry.scheme.value}


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
