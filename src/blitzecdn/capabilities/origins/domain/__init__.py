"""What the fleet says about the origins it proxies to."""

from blitzecdn.capabilities.origins.domain.published import edge_origins
from blitzecdn.capabilities.origins.domain.report import (
    EdgeOriginChecks,
    OriginReport,
)

__all__ = ["EdgeOriginChecks", "OriginReport", "edge_origins"]
