"""How the edges capability is built.

The same shape as :mod:`blitzecdn.capabilities.sites.composition`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from blitzecdn.capabilities.edges.ports import EdgeRunner, EdgeStore
from blitzecdn.capabilities.edges.service import EdgeOperationsService

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from blitzecdn.bootstrap import ControlPlane

__all__ = ["build_edge_operations_service"]


def build_edge_operations_service(
    platform: ControlPlane, *, edges: EdgeStore, runner: EdgeRunner
) -> EdgeOperationsService:
    """Wire the service that owns the roster and what runs against it.

    ``runner`` is the Ansible adapter through this capability's own narrow
    port, not ``platform.fleet``. The published one runs a named play and knows
    nothing about what a play is for, which is right for a package and too
    little for the capability that owns edge operations.
    """
    return EdgeOperationsService(
        events=platform.events,
        runner=runner,
        edges=edges,
        uow=platform.transactions,
    )
