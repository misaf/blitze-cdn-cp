"""What an edge is, and what one can tell you about a site's origin.

Two modules because there are two value types with different lifetimes: an
`Edge` is desired state an operator declares, and an `OriginCheck` is one
answer about one origin at one instant. The package is the capability's public
face, so `edges.domain` is the whole of what another capability imports.

A directory is what says that. `origins.py` a level higher would be a second
name a capability could import from `edges`, and would need a rule in the
layering test — `origins` listed beside `domain` and `policy` as a public
module — to say what the directory says by itself.
"""

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
