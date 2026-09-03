"""Edge fleet public contracts."""

from blitzecdn.capabilities.edges.domain import Edge, EdgePatch
from blitzecdn.capabilities.edges.origins import OriginCheck
from blitzecdn.capabilities.edges.probe import OriginProbe as OriginProbeAdapter
from blitzecdn.capabilities.edges.service import EdgeOperationsService

__all__ = [
    "Edge",
    "EdgeOperationsService",
    "EdgePatch",
    "OriginCheck",
    "OriginProbeAdapter",
]
