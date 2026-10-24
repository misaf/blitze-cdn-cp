"""Register the application's http3 implementation.

All code and resources ship with BlitzeCDN. Registration contributes delivery,
configuration or concrete edge resources; business operations remain services.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from blitzecdn.core.plugins import (
    AnsibleContribution,
    FleetStateContribution,
    NginxContribution,
    PluginMetadata,
    hookimpl,
)
from blitzecdn.core.runtime.resources import package_directory
from blitzecdn.providers.http3 import __version__, ansible

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from blitzecdn.capabilities.serving.domain import CdnSite
    from blitzecdn.composition import ControlPlane

#: The two fleet variables this capability owns the value of. Named once
#: because they are both contributed and declared as overrides, and the two
#: lists disagreeing is how a merge conflict at deploy time would be written.
QUIC_FLEET_VARIABLES = frozenset(
    {"blitzecdn_edge_http3_enabled", "blitzecdn_nginx_http3_listener_owner"}
)


#: The Jinja fragments this capability contributes to the edge's Nginx
#: configuration, resolved under the same guard its roles are. A sibling of
#: ``ansible/`` rather than a child: core's ``blitzecdn_nginx`` renders these
#: from the resolved contribution, so they are not part of any role this
#: package ships.
NGINX_TEMPLATES = (
    package_directory(
        __name__,
        resolves="Nginx templates are rendered from a filesystem path",
    )
    / "nginx"
)


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="http3",
        version=__version__,
        api_version=1,
        required=True,
        provides=frozenset({"http3"}),
        summary="Visitor HTTP/3 over QUIC, and the edge's single QUIC listener.",
    )


@hookimpl
def blitzecdn_nginx_contributions() -> Sequence[NginxContribution]:
    return (
        NginxContribution(
            plugin="http3",
            templates_path=NGINX_TEMPLATES,
            server_fragments=("http3-server.conf.j2",),
            upstream_fragments=("http3-upstream.conf.j2",),
        ),
    )


@hookimpl
def blitzecdn_ansible_contributions() -> Sequence[AnsibleContribution]:
    return (
        AnsibleContribution(
            plugin="http3",
            roles_path=ansible.ROLES_PATH,
            edge_roles=(ansible.EDGE_ROLE,),
        ),
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

    A disabled site converges no server block, so it cannot be the owner and
    cannot be the reason the listener opens.
    """
    enabled = sorted(
        site.name
        for site in sites
        if site.policy.enabled and site.policy.protocols.http3_enabled
    )
    return FleetStateContribution(
        plugin="http3",
        variables={
            "blitzecdn_edge_http3_enabled": bool(enabled),
            "blitzecdn_nginx_http3_listener_owner": enabled[0] if enabled else "",
        },
        overrides=QUIC_FLEET_VARIABLES,
    )
