"""Edge request filtering and emergency mitigation: the contract.

Contract only, and the one capability with *two* implementations behind it.
The rules themselves are enforced on the edge, so they ship detached:
``blitzecdn-security`` carries the firewall configuration and Under Attack
Mode, and ``blitzecdn-geoip`` carries the country database and the country
header, which is a second wheel because a country rule needs a lookup table an
installation may not want to ship. What lives here is how a site asks to be
filtered, which has to load whether or not either is installed: a stored site
with firewall rules must still read back on a core-only controller, and the
deployment is refused by name through
:attr:`~blitzecdn.core.domain.policy.CapabilityPolicy.capability_requirements`
rather than by failing to parse.

The firewall (source, country, method and path rules) and Under Attack Mode are
two faces of one capability: both decide whether a request reaches the origin,
both are evaluated in the same nginx request phase, and their ordering relative
to each other is load-bearing. Under Attack Mode is a *mode* of this capability
rather than a capability — it has no model, no store and no API of its own, only a
switch on a site and a fleet capability the edge must already carry.

The same split as ``cache``, ``compression``, ``http`` and ``tls``, for the
same reason.
"""

from blitzecdn.capabilities.security.policy import SecurityPolicy, SiteFirewall

__all__ = ["SecurityPolicy", "SiteFirewall"]
