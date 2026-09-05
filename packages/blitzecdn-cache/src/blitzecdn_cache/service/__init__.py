"""What this capability decides: which purge to run, and what to report.

`cache.py` is the service itself — expanding a purge across the fleet, and
turning what the edges logged into a statistics report.
"""

from blitzecdn_cache.service.cache import CacheService

__all__ = ["CacheService"]
