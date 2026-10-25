"""Compose the cache services from their declared collaborators."""

from __future__ import annotations

from blitzecdn.capabilities.cache.adapters import CachePlaybooks
from blitzecdn.capabilities.cache.service import CacheService
from blitzecdn.capabilities.dns.ports import SiteReader
from blitzecdn.core.ports.clock import Clock, system_clock
from blitzecdn.core.ports.operations import EventRecorder, PlaybookRunner
from blitzecdn.core.runtime.resources import distribution_version

#: The installed distribution's version, read from package metadata.
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
        # Read-only: purging never changes desired state.
        sites=sites,
        events=events,
        runner=CachePlaybooks(fleet),
        clock=clock,
    )


__all__ = ["__version__", "build_cache_service"]
