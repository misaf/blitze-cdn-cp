"""What the Automatic SSL/TLS scan needs from outside.

Sites come from ``sites``, origin probes from ``edges`` and ``origins``, and
deployments from ``deployments``, each through that capability's own ports.
The one write it makes is declared here, since a port belongs to its caller;
TLS's site editor satisfies it.
"""

from __future__ import annotations

from typing import Protocol

from blitzecdn.capabilities.tls.contract import SslMode
from blitzecdn.core.domain.site import CdnSite

__all__ = ["SslModeUpgrades"]


class SslModeUpgrades(Protocol):
    """Raise one host's SSL mode on the zone or rule that authored it.

    Returns ``None`` when the policy changed since the scan read it, so an
    upgrade never overwrites an operator's newer choice.
    """

    def apply_automatic_ssl_upgrade(
        self, site_name: str, target: SslMode, operator: str
    ) -> CdnSite | None:
        """Move a host still enrolled in Automatic SSL up to ``target``.

        ``None`` when nothing was written, because the host left Automatic SSL or
        already serves a stronger mode.
        """
        ...
