from __future__ import annotations

from typing import Protocol

from blitzecdn.capabilities.edges.domain import Edge
from blitzecdn.capabilities.edges.domain.origins import OriginCheck
from blitzecdn.core.domain.runs import AnsibleRun
from blitzecdn.core.domain.site import CdnSite


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


__all__ = ["EdgeRunner", "EdgeStore", "OriginProbe"]
