"""What the HTTP/3 provider renders: the fleet's single QUIC listener."""

from __future__ import annotations

from typing import TYPE_CHECKING

from blitzecdn.core.features import FeatureRendering, FleetStateContribution

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from blitzecdn.capabilities.serving.domain import CdnSite

#: The two fleet variables this capability owns the value of. Named once
#: because they are both contributed and declared as overrides, and the two
#: lists disagreeing is how a merge conflict at deploy time would be written.
QUIC_FLEET_VARIABLES = frozenset(
    {"blitzecdn_edge_http3_enabled", "blitzecdn_nginx_http3_listener_owner"}
)


def quic_listener(sites: tuple[CdnSite, ...]) -> FleetStateContribution:
    """Enable QUIC fleet-wide and select exactly one Nginx listener owner.

    Nginx accepts ``reuseport`` on one server block only, so the owner is named
    rather than left to whichever site rendered first. Sorted by name so the
    same fleet always picks the same owner and the desired-state document is
    byte-identical between runs.

    A disabled site converges no server block, so it cannot be the owner and
    cannot be the reason the listener opens.
    """
    enabled = sorted(
        site.name
        for site in sites
        if site.policy.enabled and site.policy.protocols.http3_enabled
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
