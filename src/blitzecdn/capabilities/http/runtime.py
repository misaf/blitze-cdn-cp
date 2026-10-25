"""What the HTTP feature renders: the fleet's baseline listener stance."""

from __future__ import annotations

from typing import TYPE_CHECKING

from blitzecdn.core.features import FeatureRendering, FleetStateContribution

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from blitzecdn.capabilities.serving.domain import CdnSite


def baseline_listeners(_sites: tuple[CdnSite, ...]) -> FleetStateContribution:
    """State the baseline: the fleet opens no QUIC listener and names no owner.

    Constant, and deliberately so. Deriving anything from the sites here would
    put HTTP/3 behavior in a capability that does not own it, and would produce
    a second source of truth for the HTTP/3 provider's listener state.
    """
    return FleetStateContribution(
        feature="http",
        variables={
            "blitzecdn_edge_http3_enabled": False,
            "blitzecdn_nginx_http3_listener_owner": "",
        },
    )


RENDERING = FeatureRendering(feature="http", fleet_state=baseline_listeners)
