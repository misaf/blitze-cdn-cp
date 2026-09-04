"""Translate domain values into the documents consumed by Ansible."""

from __future__ import annotations

from typing import Any

from blitzecdn.capabilities.edges.domain import Edge
from blitzecdn.capabilities.sites.domain import CdnSite
from blitzecdn.core.domain.validation import OmittedWhenEmpty


def site_to_ansible(site: CdnSite) -> dict[str, Any]:
    """The flat site document, minus every block that declares itself absent.

    The pruning is by declaration rather than by name. This used to read ``if
    site.firewall.empty``, which is a capability's own vocabulary in a generic
    adapter: core knew what a firewall was, and a second such block would have
    been a second branch here. A block opts in by subclassing
    :class:`~blitzecdn.core.domain.validation.OmittedWhenEmpty`, and this asks nothing
    about what it holds.
    """
    document = site.model_dump(mode="json", exclude_none=True)
    for field in type(site).model_fields:
        value = getattr(site, field)
        if isinstance(value, OmittedWhenEmpty) and value.empty:
            document.pop(field, None)
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
