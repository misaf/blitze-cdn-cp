"""What a purge is (`purge`), and what the cache is doing (`statistics`)."""

from blitzecdn.capabilities.cache.domain.purge import PurgeEntry, PurgeResult
from blitzecdn.capabilities.cache.domain.statistics import (
    CACHE_CONSULTED_OUTCOMES,
    CACHE_HIT_OUTCOMES,
    CacheStatsReport,
    EdgeStats,
    SiteCacheStats,
)

__all__ = [
    "CACHE_CONSULTED_OUTCOMES",
    "CACHE_HIT_OUTCOMES",
    "CacheStatsReport",
    "EdgeStats",
    "PurgeEntry",
    "PurgeResult",
    "SiteCacheStats",
]
