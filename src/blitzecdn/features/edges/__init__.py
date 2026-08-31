"""Edge fleet public contracts."""

from blitzecdn.features.edges.domain import Edge, EdgePatch
from blitzecdn.features.edges.origins import OriginCheck, OriginReport
from blitzecdn.features.edges.probe import OriginProbe as OriginProbeAdapter
from blitzecdn.features.edges.reporting import edge_origins
from blitzecdn.features.edges.service import EdgeOperationsService

__all__ = [
    "Edge",
    "EdgeOperationsService",
    "EdgePatch",
    "OriginCheck",
    "OriginProbeAdapter",
    "OriginReport",
    "edge_origins",
]
