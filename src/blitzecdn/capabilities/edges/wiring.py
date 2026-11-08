"""How the edges capability is built."""

from __future__ import annotations

from blitzecdn.capabilities.edges.adapters.playbooks import DecommissionPlaybook
from blitzecdn.capabilities.edges.ports import EdgeStore, FleetPlaybooks, HostKeyStore
from blitzecdn.capabilities.edges.service import EdgeOperationsService
from blitzecdn.capabilities.edges.settings import DECOMMISSION_PLAYBOOK
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.ports.operations import EventRecorder

__all__ = ["build_edge_operations_service"]


def build_edge_operations_service(
    *,
    edges: EdgeStore,
    fleet: FleetPlaybooks,
    host_keys: HostKeyStore,
    events: EventRecorder,
    uow: UnitOfWork,
) -> EdgeOperationsService:
    """Wire the fleet service, with the teardown play behind its narrow port."""
    return EdgeOperationsService(
        events=events,
        runner=DecommissionPlaybook(fleet, DECOMMISSION_PLAYBOOK),
        edges=edges,
        host_keys=host_keys,
        uow=uow,
    )
