# Copyright (c) BlitzeCDN
# GNU General Public License v3.0+
"""Publish the BlitzeCDN fleet to Ansible, straight from the control plane.

The `edges` table is the one place a host exists; this reads it each run, so
no inventory file can drift. Standard library only, since `ansible-playbook`
may run under an interpreter without `blitzecdn`. The selected columns are the
whole contract. Opened read-only (`mode=ro`), so it can never take a write
lock during a concurrent deploy.
"""

import ipaddress
import json
import os
import sqlite3

from ansible.errors import AnsibleParserError
from ansible.plugins.inventory import BaseInventoryPlugin

DOCUMENTATION = """
    name: blitzecdn
    short_description: BlitzeCDN edge servers from the control-plane database
    description:
      - Reads the C(edges) table of the BlitzeCDN control-plane SQLite database
        and publishes every edge into the C(blitzecdn_edges) group.
      - The database is the single source of truth for the fleet. Add and
        remove hosts with C(blitzecdn edge add) and C(blitzecdn edge remove);
        there is no inventory file to edit.
      - Opens the database read-only, so it is safe to run while a deployment
        is writing.
    options:
      plugin:
        description: Token that ensures this is a source file for this plugin.
        required: true
        choices: ['blitzecdn']
      database:
        description:
          - Path to the control-plane SQLite database.
          - Defaults to the C(BLITZE_DATABASE_PATH) environment variable, which
            the control plane sets for every run it starts itself.
          - With neither set, falls back to C(.state/control-plane.db) relative
            to the project root, which is where a default installation keeps
            it. That fallback is what lets C(ansible-playbook),
            C(ansible-inventory) and C(ansible-lint) be run by hand without
            anyone having to export anything.
        type: path
        required: false
        env:
          - name: BLITZE_DATABASE_PATH
      strict:
        description:
          - When C(true), a missing or unreadable database is an error.
          - When C(false), it yields an empty fleet, so C(ansible-inventory)
            can be run against a control plane that has not been set up yet.
        type: bool
        default: true
"""

EXAMPLES = """
# ansible/inventory/blitzecdn.yml
plugin: blitzecdn

# Every edge, as the control plane has it:
#   ansible-inventory -i ansible/inventory/blitzecdn.yml --list
"""

#: The group the playbooks target. Mirrors ``domain.edges.EDGE_GROUP``.
EDGE_GROUP = "blitzecdn_edges"

#: Files that mark a project root: its configuration, or a checkout before
#: `blitzecdn setup` has written one.
_PROJECT_MARKERS = ("blitzecdn.toml", "pyproject.toml")

#: Mirrors ``RESERVED_ANSIBLE_SETTINGS``, checked by ``test_inventory.py``. Such
#: a row should never exist, but publishing it at host precedence could close
#: the SSH port on every edge at once, so it is refused here too.
RESERVED_SETTINGS = frozenset(
    (
        "blitzecdn_firewall_ssh_port",
        "blitzecdn_firewall_ssh_sources",
        "blitzecdn_public_addresses",
        "blitzecdn_nginx_sites",
    )
)


class InventoryModule(BaseInventoryPlugin):
    NAME = "blitzecdn"

    def verify_file(self, path):
        """Claim `blitzecdn.yml` and `blitzecdn.yaml`, and nothing else."""
        if not super().verify_file(path):
            return False
        return path.endswith(("blitzecdn.yml", "blitzecdn.yaml"))

    def parse(self, inventory, loader, path, cache=True):
        super().parse(inventory, loader, path, cache)
        self._read_config_data(path)

        database = self.get_option("database") or _default_database(path)
        database = os.path.abspath(os.path.expanduser(str(database)))
        strict = self.get_option("strict")

        if not os.path.exists(database):
            if not strict:
                self.inventory.add_group(EDGE_GROUP)
                return
            raise AnsibleParserError(
                f"control-plane database does not exist: {database}. Run `blitzecdn "
                "setup`, or set strict: false to treat this as an empty fleet."
            )

        edges, settings = self._load(database)

        # Created even when empty, so a limited play reports "no hosts matched".
        self.inventory.add_group(EDGE_GROUP)

        # The union of management CIDRs as a host variable, which outranks the
        # shipped group defaults that also set it.
        sources = sorted(
            {source for edge in edges for source in edge["ssh_sources"]},
            key=_source_order,
        )

        for edge in edges:
            name = edge["name"]
            self.inventory.add_host(name, group=EDGE_GROUP)
            for key, value in _host_variables(edge).items():
                self.inventory.set_variable(name, key, value)
            # Settings at host precedence, so the database beats shipped defaults.
            for key, value in settings.items():
                self.inventory.set_variable(name, key, value)
            if sources:
                self.inventory.set_variable(
                    name, "blitzecdn_firewall_ssh_sources", sources
                )

    def _load(self, database):
        """Every edge row, as plain dictionaries, from a consistent WAL snapshot.

        The named columns are the contract; a change on the other side fails here.
        The list fields are JSON, as the ORM stores them.
        """
        uri = f"file:{_uri_path(database)}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=15)
        except sqlite3.Error as error:
            raise AnsibleParserError(
                f"cannot open the control-plane database {database}: {error}"
            ) from error
        try:
            connection.row_factory = sqlite3.Row
            try:
                rows = connection.execute(
                    "SELECT name, host, user, port, "
                    "private_key_file, public_addresses, ssh_sources "
                    "FROM edges ORDER BY name"
                ).fetchall()
                setting_rows = connection.execute(
                    "SELECT name, value FROM ansible_settings ORDER BY name"
                ).fetchall()
            except sqlite3.Error as error:
                raise AnsibleParserError(
                    f"cannot read the inventory tables in {database}: {error}. "
                    "Run `blitzecdn setup` to create a fresh database."
                ) from error
        finally:
            connection.close()

        edges = []
        for row in rows:
            edge = {
                "name": row["name"],
                "host": row["host"],
                "user": row["user"],
                "port": row["port"],
                "private_key_file": row["private_key_file"],
            }
            for column in ("public_addresses", "ssh_sources"):
                edge[column] = _json_list(row[column], row["name"], column)
            edges.append(edge)
        settings = {}
        for row in setting_rows:
            if row["name"] in RESERVED_SETTINGS:
                self.display.warning(
                    f"ignoring fleet setting {row['name']!r}: it is derived per "
                    "edge and publishing it would override every edge at once"
                )
                continue
            try:
                settings[row["name"]] = json.loads(row["value"])
            except ValueError as error:
                raise AnsibleParserError(
                    f"Ansible setting {row['name']!r} has an unreadable value: {error}"
                ) from error
        return edges, settings


def _json_list(value, edge_name, column):
    """Decode one required JSON list column."""
    if value is None:
        raise AnsibleParserError(
            f"edge {edge_name!r} has a null {column}; "
            "the current schema requires a list"
        )
    try:
        decoded = json.loads(value)
    except ValueError as error:
        raise AnsibleParserError(
            f"edge {edge_name!r} has an unreadable {column}: {error}"
        ) from error
    if not isinstance(decoded, list):
        raise AnsibleParserError(
            f"edge {edge_name!r} has a {column} that is not a list"
        )
    return decoded


def _default_database(source):
    """Where a default installation keeps the database, derived from this source.

    The nearest ancestor of the source holding a project marker, searched rather
    than counted so it survives the tree moving inside the package; the working
    directory, as ``Settings`` defaults, when none does. ``database:`` or
    ``BLITZE_DATABASE_PATH`` override it.
    """
    directory = os.path.dirname(os.path.abspath(source))
    while True:
        if any(os.path.isfile(os.path.join(directory, m)) for m in _PROJECT_MARKERS):
            return os.path.join(directory, ".state", "control-plane.db")
        parent = os.path.dirname(directory)
        if parent == directory:
            return os.path.join(os.getcwd(), ".state", "control-plane.db")
        directory = parent


def _host_variables(edge):
    """Ansible connection variables for one edge.

    Mirrors ``blitzecdn.capabilities.edges.adapters.ansible.edge_to_inventory``,
    checked by ``tests/contract/test_inventory.py``. Unset values are omitted, so
    SSH resolves keys normally rather than getting an empty identity file.
    """
    port = int(edge["port"])
    variables = {
        "ansible_host": edge["host"],
        "ansible_user": edge["user"],
        "ansible_port": port,
        # One source for the SSH port: `ansible_port` and the firewall's port must
        # never disagree, or the host is stranded.
        "blitzecdn_firewall_ssh_port": port,
    }
    private_key_file = edge["private_key_file"]
    if private_key_file:
        variables["ansible_ssh_private_key_file"] = private_key_file
    public_addresses = edge["public_addresses"]
    if public_addresses:
        variables["blitzecdn_public_addresses"] = list(public_addresses)
    return variables


def _source_order(source):
    """IPv4 before IPv6, numeric within each; invalid networks are never published."""
    try:
        network = ipaddress.ip_network(_text(source), strict=False)
    except ValueError as error:
        raise AnsibleParserError(
            f"management source {source!r} is not a CIDR network: {error}"
        ) from error
    return (network.version, network.network_address.packed, network.prefixlen)


def _text(value):
    return value if isinstance(value, str) else str(value)


def _uri_path(path):
    """Escape a filesystem path for a SQLite URI, where `?` and `#` are special."""
    return path.replace("?", "%3f").replace("#", "%23")
