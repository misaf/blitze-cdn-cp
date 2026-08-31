"""The fleet-wide HTTP/3 switch, contributed rather than hardcoded."""

from __future__ import annotations

from typing import TYPE_CHECKING

from blitzecdn import __version__
from blitzecdn.core.plugins import (
    FleetStateContribution,
    PluginMetadata,
    hookimpl,
)
from blitzecdn.features.dns.site_domain import CdnSite

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from blitzecdn.bootstrap import ControlPlane


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="http3",
        version=__version__,
        required=True,
        summary="Enable QUIC on the edges when any site asks for it.",
    )


@hookimpl
def blitzecdn_fleet_desired_state(
    sites: tuple[CdnSite, ...], platform: ControlPlane
) -> FleetStateContribution:
    """Whether the edges run QUIC at all, and which site owns the listener.

    `blitzecdn_edge_http3_enabled` used to be written twice — once for the
    nginx listener and once for the firewall's UDP/443 rule — with the edge play
    asserting the two copies agreed. One value cannot disagree with itself, so
    the assertion went with the copy.

    The listener owner stays an nginx concern: `reuseport` is accepted on
    exactly one server block, which is a rendering detail rather than a runtime
    fact the firewall or the container stack has any use for.
    """
    enabled = sorted(site.name for site in sites if site.enabled and site.http3_enabled)
    return FleetStateContribution(
        plugin="http3",
        variables={
            "blitzecdn_edge_http3_enabled": bool(enabled),
            "blitzecdn_nginx_http3_listener_owner": enabled[0] if enabled else "",
        },
    )
