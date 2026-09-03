"""Edge request filtering and emergency mitigation.

The firewall (source, country, method and path rules) and Under Attack Mode are
two faces of one capability: both decide whether a request reaches the origin,
both are evaluated in the same nginx request phase, and their ordering relative
to each other is load-bearing. Under Attack Mode is a *mode* of this capability
rather than a capability — it has no model, no store and no API of its own, only a
switch on a site and a fleet capability the edge must already carry.
"""

from blitzecdn.capabilities.security.policy import SecurityPolicy, SiteFirewall

__all__ = ["SecurityPolicy", "SiteFirewall"]
