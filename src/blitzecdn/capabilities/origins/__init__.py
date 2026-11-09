"""Origin probing from the edges that proxy to them."""

from blitzecdn.capabilities.origins.contract import OriginCheck
from blitzecdn.capabilities.origins.domain import OriginReport, edge_origins
from blitzecdn.core.runtime.resources import distribution_version

#: The installed distribution's version, read from package metadata.
__version__ = distribution_version(__name__)

__all__ = ["OriginCheck", "OriginReport", "__version__", "edge_origins"]
