"""Edge fleet public contracts."""

from blitzecdn.capabilities.edges.adapters.probe import (
    OriginProbe as OriginProbeAdapter,
)
from blitzecdn.capabilities.edges.domain import Edge, EdgePatch
from blitzecdn.capabilities.edges.ports import EdgeStore, OriginProbe
from blitzecdn.capabilities.edges.service import EdgeOperationsService

__all__ = [
    "Edge",
    "EdgeOperationsService",
    "EdgePatch",
    "EdgeStore",
    "OriginProbe",
    "OriginProbeAdapter",
]
