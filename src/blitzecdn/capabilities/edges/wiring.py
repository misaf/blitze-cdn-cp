"""How the edges capability is built."""

from __future__ import annotations

from blitzecdn.capabilities.edges.ports import EdgeRunner, EdgeStore
from blitzecdn.capabilities.edges.service import EdgeOperationsService
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.ports.operations import EventRecorder

__all__ = ["build_edge_operations_service"]


def build_edge_operations_service(
    *, edges: EdgeStore, runner: EdgeRunner, events: EventRecorder, uow: UnitOfWork
) -> EdgeOperationsService:
    """Wire the fleet service, with the runner behind this capability's narrow port."""
    return EdgeOperationsService(events=events, runner=runner, edges=edges, uow=uow)
