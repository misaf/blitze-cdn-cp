"""The fleet as core's Ansible layer reads it: `EdgeStore` adapted to `FleetRoster`."""

from __future__ import annotations

from blitzecdn.capabilities.edges.domain import EDGE_GROUP
from blitzecdn.capabilities.edges.ports import EdgeStore

__all__ = ["EdgeRoster"]


class EdgeRoster:
    """The recorded edges as host names, read fresh to match the inventory."""

    def __init__(self, edges: EdgeStore) -> None:
        self._edges = edges

    @property
    def group(self) -> str:
        return EDGE_GROUP

    def host_names(self) -> tuple[str, ...]:
        return tuple(edge.name for edge in self._edges.list_edges())
