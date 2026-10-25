"""Compose the application's cache services from their declared collaborators.

Configuration, ports and concrete adapters meet here. Services and domain
values do not discover features or construct the control plane.
"""

from __future__ import annotations

from blitzecdn.capabilities.cache.adapters import CachePlaybooks
from blitzecdn.capabilities.cache.service import CacheService
from blitzecdn.capabilities.dns.ports import SiteReader
from blitzecdn.core.ports.clock import Clock, system_clock
from blitzecdn.core.ports.operations import EventRecorder, PlaybookRunner
from blitzecdn.core.runtime.resources import distribution_version

#: This distribution's version, asked of the environment rather than
#: written down here: it is what ``FeatureMetadata.version`` reports and
#: what ``blitzecdn plugins`` shows an operator, so the one number that
#: must not drift from ``pyproject.toml`` is not copied out of it.
__version__ = distribution_version(__name__)


def build_cache_service(
    *,
    sites: SiteReader,
    events: EventRecorder,
    fleet: PlaybookRunner,
    clock: Clock = system_clock,
) -> CacheService:
    """Wire the cache capability to the fleet and the derived hosts."""
    return CacheService(
        # Read to decide which hostnames may be purged, and never written:
        # purging is not a change to desired state and must not be able to
        # become one.
        sites=sites,
        events=events,
        runner=CachePlaybooks(fleet),
        clock=clock,
    )


__all__ = ["__version__", "build_cache_service"]
