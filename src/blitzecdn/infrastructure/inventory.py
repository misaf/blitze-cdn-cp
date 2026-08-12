"""Reading the static inventory BlitzeCDN no longer writes.

The fleet lives in the ``edges`` table and reaches Ansible through the
``blitzecdn`` inventory plugin. Nothing here writes an inventory file any more;
what is left is the one-way door out of the old world.

:func:`read_legacy_inventory` parses a pre-existing
``ansible/inventory/hosts.yml`` into :class:`~blitzecdn.domain.edges.Edge`
models so ``blitzecdn setup`` can adopt an installation that predates the
plugin. It is deliberately tolerant in a way the old writer was not: the file
it reads was hand-editable and hand-edited, so it accepts the connection
variables operators actually put there rather than only the subset
``edge add`` used to write.

``LOCAL_GROUP_VARS_TEMPLATE`` stays because group variables have not moved.
They were never the control plane's to manage — they are Ansible's own
``group_vars/`` mechanism, applying to the group the plugin now creates exactly
as they applied to the group the static file used to declare.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from blitzecdn.domain.edges import EDGE_GROUP, Edge
from blitzecdn.exceptions import ConfigurationError

LOCAL_GROUP_VARS_TEMPLATE = """---
# Site-specific overrides for every edge. Untracked, so `./install.sh update`
# never touches it and an edit here never blocks an upgrade.
#
# Loaded after defaults.yml in the same directory and wins over it. Anything
# left out falls through to the shipped default — copy only the keys you are
# changing.

# Per-hostname country filtering. Needs a MaxMind database on the edge; set
# BLITZE_MAXMIND_ACCOUNT_ID and BLITZE_MAXMIND_LICENSE_KEY in .env and the
# control plane forwards them. A site carrying country rules fails the deploy
# while this is false rather than serving the traffic it was told to block.
# blitzecdn_nginx_geoip_enabled: true

# NOTE: blitzecdn_firewall_ssh_sources and blitzecdn_firewall_ssh_port are no
# longer set here. Both are properties of an edge, so both are recorded on the
# edge and published by the inventory plugin as host variables:
#
#   blitzecdn edge add edge-01 --host 192.0.2.10 --port 22 \\
#       --ssh-source 203.0.113.0/24
#   blitzecdn edge update edge-01 --ssh-source 203.0.113.0/24 --ssh-source ...
#
# A host variable outranks this file, so setting either key here now has no
# effect. It is left documented rather than silently ignored because that is
# exactly the trap the old layout set: `edge add --ssh-source` wrote to the
# inventory, group_vars outranked it, and the flag did nothing for a year.
"""


def initialize_group_vars(inventory_dir: Path) -> Path | None:
    """Create the untracked overrides file, or return None if it exists.

    Site configuration has to live somewhere the installer will not fight over.
    ``defaults.yml`` beside it is tracked and replaced wholesale on upgrade, so
    an edit there makes ``./install.sh update`` refuse with "tracked files have
    local changes" — which is how the file an operator is most likely to edit
    became the one that blocks updating.

    Ansible loads a ``group_vars/<group>/`` directory in alphabetical order, so
    ``local.yml`` is read after ``defaults.yml`` and wins.
    """
    directory = inventory_dir / f"group_vars/{EDGE_GROUP}"
    if not directory.is_dir():
        return None
    path = directory / "local.yml"
    if path.exists():
        return None
    path.write_text(LOCAL_GROUP_VARS_TEMPLATE, encoding="utf-8")
    return path


def read_legacy_inventory(path: Path) -> list[Edge]:
    """Parse a static ``hosts.yml`` into edges, for a one-time import.

    Every connection variable an operator may have hand-written is carried
    across, not only the three ``edge add`` knew how to write. A real
    installation had ``ansible_port`` and ``ansible_ssh_private_key_file`` in
    this file precisely because the CLI could not express them, and dropping
    them on import would strand the controller from its own fleet on the very
    first deploy after an upgrade.

    ``blitzecdn_firewall_ssh_sources`` is read from the group's inline ``vars``
    and given to every imported edge. That is where the old ``edge add`` put
    it, and the union across edges is what the plugin publishes, so importing
    it onto each one round-trips to the same set.
    """
    if path.is_symlink():
        raise ConfigurationError(f"refusing to load symlink: {path}")
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        group = document["all"]["children"][EDGE_GROUP]
        hosts = group.get("hosts") or {}
        variables = group.get("vars") or {}
        if not isinstance(hosts, dict) or not isinstance(variables, dict):
            raise TypeError("edge hosts and vars must be mappings")
    except (OSError, yaml.YAMLError, TypeError, KeyError, AttributeError) as exc:
        raise ConfigurationError(f"invalid inventory {path}: {exc}") from exc

    sources = _string_list(variables.get("blitzecdn_firewall_ssh_sources"))
    edges: list[Edge] = []
    for name, values in sorted(hosts.items()):
        if values is None:
            values = {}
        if not isinstance(values, dict):
            raise ConfigurationError(
                f"invalid inventory {path}: edge {name} is not a mapping"
            )
        edges.append(
            Edge.model_validate(
                {
                    "name": str(name),
                    "host": str(values.get("ansible_host", name)),
                    "user": str(values.get("ansible_user", "deploy")),
                    "port": int(values.get("ansible_port", 22)),
                    "private_key_file": _optional_text(
                        values.get("ansible_ssh_private_key_file")
                    ),
                    "public_addresses": _string_list(
                        values.get("blitzecdn_public_addresses")
                    ),
                    "ssh_sources": sources,
                }
            )
        )
    return edges


def _string_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigurationError(f"expected a list of strings, got {value!r}")
    return tuple(value)


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


__all__ = [
    "LOCAL_GROUP_VARS_TEMPLATE",
    "initialize_group_vars",
    "read_legacy_inventory",
]
