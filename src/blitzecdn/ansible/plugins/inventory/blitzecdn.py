# Copyright (c) BlitzeCDN
# GNU General Public License v3.0+
"""Publish the BlitzeCDN fleet to Ansible, straight from the control plane.

There is one place a host exists — the `edges` table — and this plugin reads it
at the start of every run. No inventory file is written, so nothing can drift
from the database, hold an edge already decommissioned, or need rewriting
atomically by every command that changes the fleet.

Deliberately dependency-free. It runs inside `ansible-playbook`, which may be a
different interpreter from the control plane's virtualenv with no `blitzecdn`
on its path, so this imports nothing but the standard library and Ansible's own
plugin base. The selected columns are the complete inventory contract, so a
schema mismatch fails at the query boundary.

Read-only, and it says so: the database is opened through a `file:...?mode=ro`
URI. An inventory plugin runs before anything else in a play, often against a
database a deploy is concurrently writing, and it has no business being able to
take a write lock — let alone hold one while Ansible resolves a few hundred
hosts.
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

#: Mirrors ``blitzecdn.core.domain.validation.RESERVED_ANSIBLE_SETTINGS``; kept in
#: step by ``tests/test_inventory.py``.
#:
#: The control plane refuses to store a setting under one of these names, so a
#: row carrying one should not exist. This refuses to *publish* it anyway,
#: because settings are set at host precedence and would therefore beat the
#: per-edge values derived below — and the damage is asymmetric. A manually
#: corrupted row would otherwise close the SSH port the next converge arrives
#: on, for every edge at once, with no way back in.
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
        """Claim `blitzecdn.yml` and `blitzecdn.yaml`, and nothing else.

        Ansible offers every enabled inventory plugin each source in turn, so a
        name check here is what stops this from being handed, say, a host_vars
        file. The `plugin:` key inside is verified by `_read_config_data`.
        """
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

        # Created even when empty, so a play limited to the group reports
        # "skipping: no hosts matched" rather than failing on an unknown
        # pattern. Those read very differently during an incident.
        self.inventory.add_group(EDGE_GROUP)

        # Every edge is given the union of every edge's management CIDRs, as a
        # host variable. It has to outrank
        # `inventory/group_vars/blitzecdn_edges/defaults.yml`, which also sets
        # this key — an inventory *group* variable loses to that file, so
        # setting it per group here would leave `blitzecdn edge add
        # --ssh-source` silently doing nothing.
        sources = sorted(
            {source for edge in edges for source in edge["ssh_sources"]},
            key=_source_order,
        )

        for edge in edges:
            name = edge["name"]
            self.inventory.add_host(name, group=EDGE_GROUP)
            for key, value in _host_variables(edge).items():
                self.inventory.set_variable(name, key, value)
            # Host variables outrank the shipped group_vars defaults. Settings
            # are fleet-wide, but publishing them at host precedence is what
            # makes the database authoritative without generating a YAML file.
            for key, value in settings.items():
                self.inventory.set_variable(name, key, value)
            if sources:
                self.inventory.set_variable(
                    name, "blitzecdn_firewall_ssh_sources", sources
                )

    def _load(self, database):
        """Every edge row, as plain dictionaries.

        The connection is read-only and `immutable=0`, so WAL still applies and
        this observes a consistent snapshot of a database being written by a
        concurrent deploy rather than a torn read.

        The columns named below are the whole of the contract with the control
        plane. Naming them is what makes a change on the other side fail here
        loudly. The two list-valued fields are JSON because that is how the ORM
        stores them.
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
    """Where a default installation keeps the database, from where we are.

    This file is `<project>/ansible/inventory/blitzecdn.yml` and the database is
    `<project>/.state/control-plane.db`, matching `blitzecdn.toml`. Deriving it
    from the source path rather than the working directory matters because
    Ansible is run from several of them — the control plane runs it with cwd set
    to `ansible/`, an operator runs it from the project root, and ansible-lint
    picks its own.

    Without this, every hand-run `ansible-playbook`, `ansible-inventory` and
    `ansible-lint` fails unless BLITZE_DATABASE_PATH was exported first, which
    is a poor trade for a value that is the same on every default install.
    An installation that keeps its state elsewhere sets `database:` or the
    environment variable, and both win over this.
    """
    inventory_dir = os.path.dirname(os.path.abspath(source))
    project = os.path.dirname(os.path.dirname(inventory_dir))
    return os.path.join(project, ".state", "control-plane.db")


def _host_variables(edge):
    """Ansible connection variables for one edge.

    Mirrors ``blitzecdn.core.ansible_mapping.edge_to_inventory``. The two are kept in
    step by ``tests/test_inventory.py``, which runs this module against a
    database the model wrote and compares the result — the only honest way to
    check an agreement between a pydantic model and a file that cannot import
    it.

    Unset values are omitted rather than emitted as null: Ansible would hand
    SSH an empty identity file instead of letting it resolve a key the usual
    way.
    """
    port = int(edge["port"])
    variables = {
        "ansible_host": edge["host"],
        "ansible_user": edge["user"],
        "ansible_port": port,
        # The same port the firewall must leave open, published from the one
        # place that knows it. These were two independently edited settings —
        # `ansible_port` in the inventory and `blitzecdn_firewall_ssh_port` in
        # group_vars — and a disagreement between them strands the host: the
        # firewall closes the port the next converge arrives on, and no later
        # deploy can reach it to put things back.
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
    """IPv4 before IPv6, numeric within each — not lexical.

    Text order puts 203.0.113.0/24 ahead of 94.183.119.90/32, which reads as a
    mistake in a rendered firewall rule and in a check-mode diff. Invalid
    networks are rejected by the current edge model and must not be published
    if a database has been edited outside it.
    """
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
    """Escape a filesystem path for use in a SQLite URI.

    `?` and `#` would otherwise start the query and fragment, so a database
    under a directory containing either would be opened at the wrong path — or,
    worse, with query parameters an operator did not write.
    """
    return path.replace("?", "%3f").replace("#", "%23")
