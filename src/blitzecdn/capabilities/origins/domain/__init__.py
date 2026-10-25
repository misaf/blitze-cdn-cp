"""What the fleet says about the origins it proxies to.

`report.py` is one edge's checks and the fleet-wide report they compose into;
`published.py` reads the document an edge publishes into the first of those.
The check runs on the fleet rather than on the controller, which is the whole
argument the module carries.
"""

from blitzecdn.capabilities.origins.domain.published import edge_origins
from blitzecdn.capabilities.origins.domain.report import (
    EdgeOriginChecks,
    OriginReport,
)

__all__ = ["EdgeOriginChecks", "OriginReport", "edge_origins"]
