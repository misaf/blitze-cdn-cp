from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any, cast

import yaml

from blitzecdn.exceptions import ConfigurationError, ConflictError
from blitzecdn.infrastructure.filesystem import atomic_write_yaml

LOCAL_GROUP_VARS_TEMPLATE = """---
# Site-specific overrides for every edge. Untracked, so `./install.sh update`
# never touches it and an edit here never blocks an upgrade.
#
# Loaded after defaults.yml in the same directory and wins over it. Anything
# left out falls through to the shipped default — copy only the keys you are
# changing.

# Management networks allowed to reach SSH. The firewall role aborts on an
# empty list rather than opening SSH to the world.
# blitzecdn_firewall_ssh_sources:
#   - 203.0.113.8/32

# Must match ansible_port for every edge in the inventory.
# blitzecdn_firewall_ssh_port: 22

# Per-hostname country filtering. Needs a MaxMind database on the edge; set
# BLITZE_MAXMIND_ACCOUNT_ID and BLITZE_MAXMIND_LICENSE_KEY in .env and the
# control plane forwards them. A site carrying country rules fails the deploy
# while this is false rather than serving the traffic it was told to block.
# blitzecdn_nginx_geoip_enabled: true
"""


class Inventory:
    """Small, validated facade over the operator-facing Ansible inventory."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> bool:
        if self.path.exists():
            return False
        self._write(self._empty())
        return True

    def initialize_group_vars(self) -> Path | None:
        """Create the untracked overrides file, or return None if it exists.

        Site configuration has to live somewhere the installer will not fight
        over. `defaults.yml` beside this is tracked and replaced wholesale on
        upgrade, so an edit there makes `./install.sh update` refuse with
        "tracked files have local changes" — which is how the file an operator
        is most likely to edit became the one that blocks updating.

        Ansible loads a `group_vars/<group>/` directory in alphabetical order,
        so `local.yml` is read after `defaults.yml` and wins.
        """
        directory = self.path.parent / "group_vars/blitzecdn_edges"
        if not directory.is_dir():
            return None
        path = directory / "local.yml"
        if path.exists():
            return None
        path.write_text(LOCAL_GROUP_VARS_TEMPLATE, encoding="utf-8")
        return path

    def list_edges(self) -> list[dict[str, Any]]:
        hosts = self._edge_group(self._read())["hosts"]
        edges: list[dict[str, Any]] = []
        for name, values in sorted(hosts.items()):
            edge: dict[str, Any] = {
                "name": str(name),
                "host": str(values.get("ansible_host", name)),
                "user": str(values.get("ansible_user", "deploy")),
            }
            public_addresses = values.get("blitzecdn_public_addresses")
            if public_addresses is not None:
                if not isinstance(public_addresses, list) or not all(
                    isinstance(value, str) for value in public_addresses
                ):
                    raise ConfigurationError(
                        f"invalid public addresses for edge: {name}"
                    )
                edge["public_addresses"] = public_addresses
            edges.append(edge)
        return edges

    def add_edge(
        self,
        name: str,
        *,
        host: str,
        user: str,
        ssh_sources: list[str],
        public_addresses: list[str] | None = None,
    ) -> dict[str, Any]:
        for label, value in (
            ("edge name", name),
            ("edge host", host),
            ("SSH user", user),
        ):
            if not value.strip() or any(char.isspace() for char in value):
                raise ConfigurationError(
                    f"{label} cannot be empty or contain whitespace"
                )
        try:
            normalized_sources = [
                str(ipaddress.ip_network(source, strict=False))
                for source in ssh_sources
            ]
        except ValueError as exc:
            raise ConfigurationError(f"invalid management CIDR: {exc}") from exc
        normalized_public_addresses = list(dict.fromkeys(public_addresses or []))
        if any(
            not value.strip() or any(char.isspace() for char in value)
            for value in normalized_public_addresses
        ):
            raise ConfigurationError(
                "public edge addresses cannot be empty or contain whitespace"
            )
        document = self._read() if self.path.exists() else self._empty()
        group = self._edge_group(document)
        if name in group["hosts"]:
            raise ConflictError(f"edge already exists: {name}")
        host_values: dict[str, Any] = {
            "ansible_host": host,
            "ansible_user": user,
        }
        if normalized_public_addresses:
            host_values["blitzecdn_public_addresses"] = normalized_public_addresses
        group["hosts"][name] = host_values
        existing = group["vars"].get("blitzecdn_firewall_ssh_sources", [])
        group["vars"]["blitzecdn_firewall_ssh_sources"] = list(
            dict.fromkeys([*existing, *normalized_sources])
        )
        self._write(document)
        result: dict[str, Any] = {"name": name, "host": host, "user": user}
        if normalized_public_addresses:
            result["public_addresses"] = normalized_public_addresses
        return result

    def remove_edge(self, name: str) -> None:
        document = self._read()
        hosts = self._edge_group(document)["hosts"]
        if name not in hosts:
            raise ConfigurationError(f"edge does not exist: {name}")
        del hosts[name]
        self._write(document)

    def set_public_addresses(
        self, name: str, public_addresses: list[str]
    ) -> dict[str, Any]:
        normalized = list(dict.fromkeys(public_addresses))
        if not normalized or any(
            not value.strip() or any(char.isspace() for char in value)
            for value in normalized
        ):
            raise ConfigurationError(
                "at least one public edge address without whitespace is required"
            )
        document = self._read()
        hosts = self._edge_group(document)["hosts"]
        if name not in hosts:
            raise ConfigurationError(f"edge does not exist: {name}")
        hosts[name]["blitzecdn_public_addresses"] = normalized
        self._write(document)
        return next(edge for edge in self.list_edges() if edge["name"] == name)

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "all": {
                "children": {
                    "blitzecdn_edges": {
                        "hosts": {},
                        "vars": {"blitzecdn_firewall_ssh_sources": []},
                    }
                }
            }
        }

    def _read(self) -> dict[str, Any]:
        if self.path.is_symlink():
            raise ConfigurationError(f"refusing to load symlink: {self.path}")
        try:
            document = yaml.safe_load(self.path.read_text(encoding="utf-8"))
            self._edge_group(document)
        except (OSError, yaml.YAMLError, TypeError, KeyError) as exc:
            raise ConfigurationError(f"invalid inventory {self.path}: {exc}") from exc
        return cast(dict[str, Any], document)

    @staticmethod
    def _edge_group(document: Any) -> dict[str, Any]:
        group = document["all"]["children"]["blitzecdn_edges"]
        if not isinstance(group, dict):
            raise TypeError("blitzecdn_edges must be a mapping")
        hosts = group.setdefault("hosts", {})
        variables = group.setdefault("vars", {})
        if not isinstance(hosts, dict) or not isinstance(variables, dict):
            raise TypeError("edge hosts and vars must be mappings")
        if not all(isinstance(value, dict) for value in hosts.values()):
            raise TypeError("each edge must be a mapping")
        return group

    def _write(self, document: dict[str, Any]) -> None:
        atomic_write_yaml(self.path, document, mode=0o600)
