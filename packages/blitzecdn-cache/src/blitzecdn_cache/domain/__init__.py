"""What a purge is, and what the cache is doing.

Two modules because there are two kinds of value with nothing in common but
the capability: `purge.py` is an instruction and its per-edge outcome,
`statistics.py` is a reading taken from logs the edges wrote anyway. The
package is the capability's public face, so `blitzecdn_cache.domain` still
means what it did when it was a file.
"""

from blitzecdn_cache.domain.purge import PurgeEntry, PurgeResult
from blitzecdn_cache.domain.statistics import (
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
