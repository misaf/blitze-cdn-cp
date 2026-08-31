"""Cache operations: an optional BlitzeCDN capability, installed separately.

Purging what the edges have stored, and reporting how well the cache is
working. ``blitzecdn`` never imports this package — the control plane finds it
through its ``blitzecdn.plugins`` entry point or not at all.
"""

from blitzecdn_cache.composition import __version__, build_cache_service
from blitzecdn_cache.domain import CacheStatsReport, PurgeEntry
from blitzecdn_cache.service import CacheService

__all__ = [
    "CacheService",
    "CacheStatsReport",
    "PurgeEntry",
    "__version__",
    "build_cache_service",
]
