# Copyright (c) BlitzeCDN
# GNU General Public License v3.0+
"""Publish the BlitzeCDN fleet to Ansible, straight from the control plane.

This replaces `ansible/inventory/hosts.yml`, which the control plane used to
write and Ansible used to read. That file was a second source of truth: it
drifted when hand-edited, it could hold a host the database had already
decommissioned, and every command that changed the fleet had to remember to
rewrite it atomically. Now there is one place a host exists — the `edges` table
— and this plugin reads it at the start of every run.

Deliberately dependency-free. It runs inside `ansible-playbook`, which may be a
different interpreter from the control plane's virtualenv with no `blitzecdn`
on its path, so this imports nothing but the standard library and Ansible's own
plugin base. The consequence is that it cannot validate rows against the
pydantic model that wrote them, which is why every row carries `schema_version`
and why this refuses one it was not written for.

Read-only, and it says so: the database is opened through a `file:...?mode=ro`
URI. An inventory plugin runs before anything else in a play, often against a
database a deploy is concurrently writing, and it has no business being able to
take a write lock — let alone hold one while Ansible resolves a few hundred
hosts.
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

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

import json
import os
import sqlite3

from ansible.errors import AnsibleParserError
from ansible.plugins.inventory import BaseInventoryPlugin

#: Must match ``blitzecdn.domain.edges.EDGE_SCHEMA_VERSION``. A row written
#: with a higher version is refused rather than guessed at: the two halves ship
#: and are upgraded independently, and a control plane that has moved ahead of
#: this checkout should fail at the start of a run, not converge a fleet this
#: plugin only partly understood.
SUPPORTED_SCHEMA_VERSION = 1

#: The group the playbooks target. Mirrors ``domain.edges.EDGE_GROUP``.
EDGE_GROUP = "blitzecdn_edges"


class InventoryModule(BaseInventoryPlugin):
    NAME = "blitzecdn"

    def verify_file(self, path):
        """Claim `blitzecdn.yml` and `blitzecdn.yaml`, and nothing else.

        Ansible offers every enabled inventory plugin each source in turn, so a
        name check here is what stops this from being handed, say, a host_vars
        file. The `plugin:` key inside is verified by `_read_config_data`.
        """
        if not super(InventoryModule, self).verify_file(path):
            return False
        return path.endswith(("blitzecdn.yml", "blitzecdn.yaml"))

    def parse(self, inventory, loader, path, cache=True):
        super(InventoryModule, self).parse(inventory, loader, path, cache)
        self._read_config_data(path)

        database = self.get_option("database") or _default_database(path)
        database = os.path.abspath(os.path.expanduser(str(database)))
        strict = self.get_option("strict")

        if not os.path.exists(database):
            if not strict:
                self.inventory.add_group(EDGE_GROUP)
                return
            raise AnsibleParserError(
                "control-plane database does not exist: %s. Run `blitzecdn "
                "setup`, or set strict: false to treat this as an empty fleet."
                % database
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
        # --ssh-source` silently doing nothing, which is what it did while the
        # fleet lived in a static inventory.
        sources = sorted(
            set(
                source
                for edge in edges
                for source in edge.get("ssh_sources") or []
            ),
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
        """
        uri = "file:%s?mode=ro" % _uri_path(database)
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=15)
        except sqlite3.Error as error:
            raise AnsibleParserError(
                "cannot open the control-plane database %s: %s" % (database, error)
            )
        try:
            connection.row_factory = sqlite3.Row
            try:
                rows = connection.execute(
                    "SELECT name, schema_version, document FROM edges ORDER BY name"
                ).fetchall()
                setting_rows = connection.execute(
                    "SELECT name, document FROM ansible_settings ORDER BY name"
                ).fetchall()
            except sqlite3.Error as error:
                raise AnsibleParserError(
                    "cannot read the edges table in %s: %s. This database may "
                    "predate dynamic inventory; run `blitzecdn setup` to "
                    "migrate it." % (database, error)
                )
        finally:
            connection.close()

        edges = []
        for row in rows:
            version = row["schema_version"]
            if version > SUPPORTED_SCHEMA_VERSION:
                raise AnsibleParserError(
                    "edge %r was written with schema version %s, but this "
                    "inventory plugin understands at most %s. The Ansible tree "
                    "is older than the control plane that wrote this database; "
                    "update it before deploying."
                    % (row["name"], version, SUPPORTED_SCHEMA_VERSION)
                )
            try:
                document = json.loads(row["document"])
            except ValueError as error:
                raise AnsibleParserError(
                    "edge %r has an unreadable record: %s" % (row["name"], error)
                )
            # The primary key is the name, so trust the column over the
            # document if they ever disagree — the column is what `--limit`
            # was expanded against.
            document["name"] = row["name"]
            edges.append(document)
        settings = {}
        for row in setting_rows:
            try:
                settings[row["name"]] = json.loads(row["document"])
            except ValueError as error:
                raise AnsibleParserError(
                    "Ansible setting %r has an unreadable value: %s"
                    % (row["name"], error)
                )
        return edges, settings


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

    Mirrors ``blitzecdn.domain.edges.Edge.to_inventory``. The two are kept in
    step by ``tests/test_inventory.py``, which runs this module against a
    database the model wrote and compares the result — the only honest way to
    check an agreement between a pydantic model and a file that cannot import
    it.

    Unset values are omitted rather than emitted as null: Ansible would hand
    SSH an empty identity file instead of letting it resolve a key the usual
    way.
    """
    port = int(edge.get("port") or 22)
    variables = {
        "ansible_host": edge.get("host") or edge["name"],
        "ansible_user": edge.get("user") or "deploy",
        "ansible_port": port,
        # The same port the firewall must leave open, published from the one
        # place that knows it. These were two independently edited settings —
        # `ansible_port` in the inventory and `blitzecdn_firewall_ssh_port` in
        # group_vars — and a disagreement between them strands the host: the
        # firewall closes the port the next converge arrives on, and no later
        # deploy can reach it to put things back.
        "blitzecdn_firewall_ssh_port": port,
    }
    private_key_file = edge.get("private_key_file")
    if private_key_file:
        variables["ansible_ssh_private_key_file"] = private_key_file
    public_addresses = edge.get("public_addresses")
    if public_addresses:
        variables["blitzecdn_public_addresses"] = list(public_addresses)
    return variables


def _source_order(source):
    """IPv4 before IPv6, numeric within each — not lexical.

    Text order puts 203.0.113.0/24 ahead of 94.183.119.90/32, which reads as a
    mistake in a rendered firewall rule and in a check-mode diff. Falls back to
    text for anything unparseable so a stored value written by an older release
    can still be published and then corrected.
    """
    try:
        import ipaddress

        network = ipaddress.ip_network(_text(source), strict=False)
    except (ImportError, ValueError):
        return (9, source)
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
