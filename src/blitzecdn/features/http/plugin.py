"""Register the HTTP capability and its fleet-wide listener state.

The QUIC contribution is the reason this is a capability and not a switch on
``sites``. Which sites want HTTP/3 is per-site policy, but *whether the edge
opens a QUIC listener at all* and *which single server block carries
``reuseport``* are facts about the fleet that no one site knows about itself.
They were derived in ``sites/plugin.py`` while the ports, schemes and the
``http3_enabled`` switch they depend on lived there too; they belong with them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from blitzecdn import __version__
from blitzecdn.core.plugins import FleetStateContribution, PluginMetadata, hookimpl

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from blitzecdn.bootstrap import ControlPlane
    from blitzecdn.features.sites.domain import CdnSite


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="http",
        version=__version__,
        required=True,
        summary="Visitor HTTP protocol versions and the edge listener contract.",
    )


@hookimpl
def blitzecdn_fleet_desired_state(
    sites: tuple[CdnSite, ...], platform: ControlPlane
) -> FleetStateContribution:
    """Enable QUIC fleet-wide and select exactly one Nginx listener owner.

    Nginx accepts ``reuseport`` on one server block only, so the owner is named
    rather than left to whichever site rendered first. Sorted by name so the
    same fleet always picks the same owner and the desired-state document is
    byte-identical between runs.
    """
    enabled = sorted(site.name for site in sites if site.enabled and site.http3_enabled)
    return FleetStateContribution(
        plugin="http",
        variables={
            "blitzecdn_edge_http3_enabled": bool(enabled),
            "blitzecdn_nginx_http3_listener_owner": enabled[0] if enabled else "",
        },
    )
