from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from blitzecdn.capabilities.edges.domain import Edge, HostKey
from blitzecdn.capabilities.edges.domain.origins import OriginCheck
from blitzecdn.core.domain.runs import AnsibleRun
from blitzecdn.core.domain.site import CdnSite


class FleetPlaybooks(Protocol):
    """Run one named play across the edges in scope, without the deployment lock."""

    def run_playbook(
        self,
        *,
        name: str,
        playbook: Path,
        variables: Mapping[str, object],
        host_limit: str | None = None,
    ) -> AnsibleRun: ...


class EdgeRunner(Protocol):
    """What edge operations run against the fleet; ``host_limit`` is always required."""

    def run_decommission(self, *, host_limit: str) -> AnsibleRun: ...


class EdgeStore(Protocol):
    """The fleet; writing an edge here is publishing it to the Ansible inventory."""

    def list_edges(self) -> list[Edge]: ...

    def get_edge(self, name: str) -> Edge: ...

    def create_edge(self, edge: Edge) -> Edge: ...

    def replace_edge(self, edge: Edge, *, expected: Edge | None = None) -> Edge: ...

    def delete_edge(self, name: str) -> None: ...


class HostKeyStore(Protocol):
    """The SSH host keys the controller trusts for its edges.

    ``managed`` is false where the controller has no SSH configuration of its
    own — a checkout, where the operator's SSH client and ``known_hosts`` decide.
    """

    @property
    def managed(self) -> bool: ...

    def scan(self, host: str, port: int) -> tuple[HostKey, ...]: ...

    def trusted(self, host: str, port: int) -> tuple[HostKey, ...]: ...

    def trust(self, host: str, port: int, keys: tuple[HostKey, ...]) -> None: ...


# ----------------------------------------------------------------------
# Certificates
# ----------------------------------------------------------------------


class OriginProbe(Protocol):
    """A site's origin: how to reach it, and what the controller sees of it.

    ``to_probe`` renders it for the edges; ``check`` serves only certificate
    preflight's advisory check.
    """

    def to_probe(self, site: CdnSite) -> dict[str, object]: ...

    def check(self, site: CdnSite) -> OriginCheck: ...


__all__ = [
    "EdgeRunner",
    "EdgeStore",
    "FleetPlaybooks",
    "HostKeyStore",
    "OriginProbe",
]
