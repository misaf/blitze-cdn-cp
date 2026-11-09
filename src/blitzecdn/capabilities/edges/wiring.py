"""How the edges capability is built."""

from __future__ import annotations

from dishka import Provider, Scope, provide

from blitzecdn.capabilities.edges.adapters.playbooks import DecommissionPlaybook
from blitzecdn.capabilities.edges.ports import EdgeStore, FleetPlaybooks, HostKeyStore
from blitzecdn.capabilities.edges.service import EdgeOperationsService
from blitzecdn.capabilities.edges.settings import DECOMMISSION_PLAYBOOK
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.ports.operations import EventRecorder, PlaybookRunner

__all__ = ["EdgesProvider", "build_edge_operations_service"]


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


class EdgesProvider(Provider):
    """Fleet membership and removal.

    The edge table itself is an adapter a test may replace, so the composition
    root provides it.
    """

    scope = Scope.APP

    @provide
    def operations(
        self,
        edges: EdgeStore,
        fleet: PlaybookRunner,
        host_keys: HostKeyStore,
        events: EventRecorder,
        uow: UnitOfWork,
    ) -> EdgeOperationsService:
        return build_edge_operations_service(
            edges=edges, fleet=fleet, host_keys=host_keys, events=events, uow=uow
        )
