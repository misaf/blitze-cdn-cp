"""How the DNS capability is built.

The same shape as :mod:`blitzecdn.capabilities.sites.composition`, and the same
rule: what a package could have comes from ``platform``, what only a built-in
may have is an explicit argument.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from blitzecdn.capabilities.dns.ports import SiteHostnames, ZoneStore
from blitzecdn.capabilities.dns.service import DnsService

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from blitzecdn.bootstrap import ControlPlane

__all__ = ["build_dns_service"]


def build_dns_service(
    platform: ControlPlane, *, zones: ZoneStore, sites: SiteHostnames
) -> DnsService:
    """Wire the service that owns which hostnames route to which site.

    Both stores are explicit. ``zones`` has no published counterpart at all,
    and ``sites`` is wanted here through ``SiteHostnames`` — a wider read than
    the ``SiteReader`` core publishes, because deriving ``server_names`` means
    reading which site a record's target names.
    """
    return DnsService(
        zones=zones,
        sites=sites,
        events=platform.events,
        uow=platform.transactions,
    )
