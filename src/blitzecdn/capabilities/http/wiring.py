"""What the HTTP feature renders: the fleet's baseline listener stance."""

from __future__ import annotations

from typing import TYPE_CHECKING

from blitzecdn.core.features import FeatureRendering, FleetStateContribution

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from blitzecdn.core.domain.site import CdnSite


def baseline_listeners(_sites: tuple[CdnSite, ...]) -> FleetStateContribution:
    """The baseline: no QUIC listener and no owner, constant by design."""
    return FleetStateContribution(
        feature="http",
        variables={
            "blitzecdn_edge_http3_enabled": False,
            "blitzecdn_nginx_http3_listener_owner": "",
        },
    )


RENDERING = FeatureRendering(feature="http", fleet_state=baseline_listeners)
