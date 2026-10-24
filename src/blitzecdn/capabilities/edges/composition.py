"""How the edges capability is built.

The same shape as :mod:`blitzecdn.capabilities.dns.composition`.
"""

from __future__ import annotations

from blitzecdn.capabilities.edges.ports import EdgeRunner, EdgeStore
from blitzecdn.capabilities.edges.service import EdgeOperationsService
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.ports.operations import EventRecorder

__all__ = ["build_edge_operations_service"]


def build_edge_operations_service(
    *, edges: EdgeStore, runner: EdgeRunner, events: EventRecorder, uow: UnitOfWork
) -> EdgeOperationsService:
    """Wire the service that owns the roster and what runs against it.

    ``runner`` is the Ansible adapter through this capability's own narrow
    port, not the generic ``PlaybookRunner``. That one runs a named play and
    knows nothing about what a play is for, which is right for a package and
    too little for the capability that owns edge operations.
    """
    return EdgeOperationsService(events=events, runner=runner, edges=edges, uow=uow)
