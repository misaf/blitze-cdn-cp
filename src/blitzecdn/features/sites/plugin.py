"""Register site-serving desired-state contributions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from blitzecdn import __version__
from blitzecdn.core.ansible.mapping import site_to_ansible
from blitzecdn.core.plugins import (
    FleetStateContribution,
    PluginMetadata,
    SiteStateContribution,
    hookimpl,
)
from blitzecdn.features.sites.domain import CdnSite

if TYPE_CHECKING:  # pragma: no cover - typing only
    from blitzecdn.bootstrap import ControlPlane


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="sites",
        version=__version__,
        required=True,
        summary="Site-serving policy and its edge desired state.",
    )


@hookimpl
def blitzecdn_site_desired_state(site: CdnSite) -> SiteStateContribution:
    """Project the stable flat site contract consumed by the edge roles."""
    return SiteStateContribution(plugin="sites", variables=site_to_ansible(site))


@hookimpl
def blitzecdn_fleet_desired_state(
    sites: tuple[CdnSite, ...], platform: ControlPlane
) -> FleetStateContribution:
    """Enable QUIC fleet-wide and select exactly one Nginx listener owner."""
    enabled = sorted(site.name for site in sites if site.enabled and site.http3_enabled)
    return FleetStateContribution(
        plugin="sites",
        variables={
            "blitzecdn_edge_http3_enabled": bool(enabled),
            "blitzecdn_nginx_http3_listener_owner": enabled[0] if enabled else "",
        },
    )
