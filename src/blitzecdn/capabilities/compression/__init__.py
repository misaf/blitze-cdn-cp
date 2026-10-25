"""Response compression policy and edge resources.

Shipped with the application. Contracts stay pure; services and concrete
providers are assembled explicitly by composition.
"""

from blitzecdn.capabilities.compression.contract import (
    CompressionMode,
    CompressionPolicy,
)

__all__ = ["CompressionMode", "CompressionPolicy"]
