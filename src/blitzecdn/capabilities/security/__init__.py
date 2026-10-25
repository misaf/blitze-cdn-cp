"""Request filtering policy and edge enforcement.

Shipped with the application. Contracts stay pure; services and concrete
providers are assembled explicitly by composition.
"""

from blitzecdn.capabilities.security.contract import SecurityPolicy, SiteFirewall

__all__ = ["SecurityPolicy", "SiteFirewall"]
