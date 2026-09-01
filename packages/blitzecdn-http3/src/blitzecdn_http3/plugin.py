"""Register HTTP/3 and derive the fleet's QUIC listener state.

The stable control-plane contract carries ``http3_enabled``. This optional
package owns the fleet-level behavior that makes the switch operational: opening
QUIC and selecting the one enabled server block that carries ``reuseport``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from blitzecdn.core.plugins import FleetStateContribution, PluginMetadata, hookimpl
from blitzecdn_http3 import __version__

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from blitzecdn.bootstrap import ControlPlane
    from blitzecdn.features.sites.domain import CdnSite

QUIC_FLEET_VARIABLES = frozenset(
    {"blitzecdn_edge_http3_enabled", "blitzecdn_nginx_http3_listener_owner"}
)


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="http3",
        version=__version__,
        summary="Visitor HTTP/3 over QUIC, and the edge's single QUIC listener.",
    )


@hookimpl
def blitzecdn_fleet_desired_state(
    sites: tuple[CdnSite, ...], platform: ControlPlane
) -> FleetStateContribution:
    enabled = sorted(site.name for site in sites if site.enabled and site.http3_enabled)
    return FleetStateContribution(
        plugin="http3",
        variables={
            "blitzecdn_edge_http3_enabled": bool(enabled),
            "blitzecdn_nginx_http3_listener_owner": enabled[0] if enabled else "",
        },
        overrides=QUIC_FLEET_VARIABLES,
    )
