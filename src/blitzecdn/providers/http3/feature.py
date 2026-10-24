"""Declare the application's HTTP/3 provider."""

from __future__ import annotations

from blitzecdn.core.plugins import (
    AnsibleContribution,
    Feature,
    NginxContribution,
    PluginMetadata,
)
from blitzecdn.core.runtime.resources import package_directory
from blitzecdn.providers.http3 import __version__, ansible

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

FEATURES = (
    Feature(
        metadata=PluginMetadata(
            name="http3",
            version=__version__,
            api_version=2,
            required=True,
            provides=frozenset({"http3"}),
            summary="Visitor HTTP/3 over QUIC, and the edge's single QUIC listener.",
        ),
        ansible=(
            AnsibleContribution(
                plugin="http3",
                roles_path=ansible.ROLES_PATH,
                edge_roles=(ansible.EDGE_ROLE,),
            ),
        ),
        nginx=(
            NginxContribution(
                plugin="http3",
                templates_path=NGINX_TEMPLATES,
                server_fragments=("http3-server.conf.j2",),
                upstream_fragments=("http3-upstream.conf.j2",),
            ),
        ),
    ),
)
