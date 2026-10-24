"""Cache policy and cache operations.

Shipped with the application. Contracts stay pure; services and concrete
providers are assembled explicitly by composition.
"""

from blitzecdn.capabilities.cache.policy import CachePolicy, CacheQueryStringMode

__all__ = ["CachePolicy", "CacheQueryStringMode"]
