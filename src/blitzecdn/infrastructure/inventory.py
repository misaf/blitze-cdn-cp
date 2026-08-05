from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any, cast

import yaml

from blitzecdn.exceptions import ConfigurationError, ConflictError
from blitzecdn.infrastructure.filesystem import atomic_write_yaml


class Inventory:
    """Small, validated facade over the operator-facing Ansible inventory."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> bool:
        if self.path.exists():
            return False
        self._write(self._empty())
        return True

    def list_edges(self) -> list[dict[str, str]]:
        hosts = self._edge_group(self._read())["hosts"]
        return [
            {
                "name": str(name),
                "host": str(values.get("ansible_host", name)),
                "user": str(values.get("ansible_user", "deploy")),
            }
            for name, values in sorted(hosts.items())
        ]

    def add_edge(
        self, name: str, *, host: str, user: str, ssh_sources: list[str]
    ) -> dict[str, str]:
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
        document = self._read() if self.path.exists() else self._empty()
        group = self._edge_group(document)
        if name in group["hosts"]:
            raise ConflictError(f"edge already exists: {name}")
        group["hosts"][name] = {"ansible_host": host, "ansible_user": user}
        existing = group["vars"].get("blitzecdn_firewall_ssh_sources", [])
        group["vars"]["blitzecdn_firewall_ssh_sources"] = list(
            dict.fromkeys([*existing, *normalized_sources])
        )
        self._write(document)
        return {"name": name, "host": host, "user": user}

    def remove_edge(self, name: str) -> None:
        document = self._read()
        hosts = self._edge_group(document)["hosts"]
        if name not in hosts:
            raise ConfigurationError(f"edge does not exist: {name}")
        del hosts[name]
        self._write(document)

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
