"""Register the baseline HTTP capability and the fleet's listener stance.

HTTP/1.1 and HTTP/2 are invariants of the managed edge: every edge serves
them, nothing turns them on, and no distribution has to be installed for them
to work. That is the whole of what this plugin owns.

The two QUIC variables are contributed here at their *baseline* — no listener,
no owner — rather than left out. They are `required: true` in the
``blitzecdn_edge`` and ``blitzecdn_nginx`` argument specs, and the desired-state
document an operator reads should say what the fleet's listener stance is in
every installation rather than only in the ones that happen to have HTTP/3
attached. ``blitzecdn-http3`` declares both in its ``overrides`` and replaces
them when it is installed, which is the same mechanism ``certificates`` uses
for the certificate paths ``sites`` projects.

So the document has one shape whichever distributions are present, and the
difference between "HTTP/3 is not installed" and "no site asked for HTTP/3" is
invisible to the edge — correctly, because the edge does the same thing in
both cases. The difference is made visible where it belongs: a site that asks
for HTTP/3 without the capability installed is refused by name at validation,
before any of this is rendered.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from blitzecdn import __version__
from blitzecdn.core.plugins import FleetStateContribution, PluginMetadata, hookimpl

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from blitzecdn.bootstrap import ControlPlane
    from blitzecdn.capabilities.sites.domain import CdnSite


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="http",
        version=__version__,
        required=True,
        summary="Baseline visitor HTTP/1.1 and HTTP/2, and the listener contract.",
    )


@hookimpl
def blitzecdn_fleet_desired_state(
    sites: tuple[CdnSite, ...], platform: ControlPlane
) -> FleetStateContribution:
    """State the baseline: the fleet opens no QUIC listener and names no owner.

    Constant, and deliberately so. Deriving anything from `sites` here would be
    HTTP/3 behavior living in the capability that no longer owns it, and it
    would produce a fleet document that disagreed with itself the moment
    ``blitzecdn-http3`` was detached from a controller whose sites still asked
    for it.
    """
    return FleetStateContribution(
        plugin="http",
        variables={
            "blitzecdn_edge_http3_enabled": False,
            "blitzecdn_nginx_http3_listener_owner": "",
        },
    )
