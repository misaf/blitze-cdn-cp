"""The fleet as core's Ansible layer reads it.

`EdgeStore` is this capability's own read/write port and names `Edge` all
through it; `FleetRoster` is the two facts core needs to expand a ``--limit``.
This is the adapter between them, and it exists so that the arrow points the
way the layers do: `core.ansible` declared the port, `edges` satisfies it, and
core no longer imports a capability to run a playbook.
"""

from __future__ import annotations

from blitzecdn.capabilities.edges.domain import EDGE_GROUP
from blitzecdn.capabilities.edges.ports import EdgeStore

__all__ = ["EdgeRoster"]


class EdgeRoster:
    """The recorded edges, as host names.

    Reads the store on every call rather than caching. The whole reason a
    limit is expanded against the database instead of handed to Ansible as a
    pattern is that the answer must come from the rows the inventory plugin is
    about to read; a roster that remembered an earlier answer would reintroduce
    exactly the drift the static inventory file had.
    """

    def __init__(self, edges: EdgeStore) -> None:
        self._edges = edges

    @property
    def group(self) -> str:
        return EDGE_GROUP

    def host_names(self) -> tuple[str, ...]:
        return tuple(edge.name for edge in self._edges.list_edges())
