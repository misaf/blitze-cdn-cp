"""What the HTTP/3 provider renders: the fleet's single QUIC listener."""

from __future__ import annotations

from typing import TYPE_CHECKING

from blitzecdn.capabilities.http.contract import ProtocolPolicy
from blitzecdn.core.features import FeatureRendering, FleetStateContribution

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from blitzecdn.capabilities.serving.domain import CdnSite

#: The two fleet variables this feature sets and overrides, named once.
QUIC_FLEET_VARIABLES = frozenset(
    {"blitzecdn_edge_http3_enabled", "blitzecdn_nginx_http3_listener_owner"}
)


def quic_listener(sites: tuple[CdnSite, ...]) -> FleetStateContribution:
    """Enable QUIC fleet-wide and pick exactly one listener owner.

    Nginx takes ``reuseport`` on one server block only; the first enabled site by
    name owns it, so the document is stable.
    """
    enabled = sorted(
        site.name
        for site in sites
        if site.policy.enabled and site.policy[ProtocolPolicy].http3_enabled
    )
    return FleetStateContribution(
        feature="http3",
        variables={
            "blitzecdn_edge_http3_enabled": bool(enabled),
            "blitzecdn_nginx_http3_listener_owner": enabled[0] if enabled else "",
        },
        overrides=QUIC_FLEET_VARIABLES,
    )


RENDERING = FeatureRendering(feature="http3", fleet_state=quic_listener)
