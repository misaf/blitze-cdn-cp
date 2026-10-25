"""What an edge is (`Edge`), and one answer about a site's origin (`OriginCheck`)."""

from blitzecdn.capabilities.edges.domain.edge import (
    EDGE_GROUP,
    EDGE_NAME,
    Edge,
    EdgePatch,
    firewall_sources,
)
from blitzecdn.capabilities.edges.domain.origins import OriginCheck

__all__ = [
    "EDGE_GROUP",
    "EDGE_NAME",
    "Edge",
    "EdgePatch",
    "OriginCheck",
    "firewall_sources",
]
