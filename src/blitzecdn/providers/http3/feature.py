"""Declare the application's HTTP/3 provider."""

from __future__ import annotations

from blitzecdn.core.features import (
    AnsibleContribution,
    Feature,
    FeatureMetadata,
    NginxContribution,
)
from blitzecdn.core.runtime.resources import package_directory
from blitzecdn.providers.http3 import __version__, ansible

#: The Jinja fragments this capability contributes to the edge's Nginx
#: configuration, resolved under the same guard its roles are. A sibling of
#: ``ansible/`` rather than a child: the controller renders these into each
#: release's configuration, so they are not part of any role this package
#: ships.
NGINX_TEMPLATES = (
    package_directory(
        __name__,
        resolves="Nginx templates are rendered from a filesystem path",
    )
    / "nginx"
)

FEATURES = (
    Feature(
        metadata=FeatureMetadata(
            name="http3",
            version=__version__,
            provides=frozenset({"http3"}),
            summary="Visitor HTTP/3 over QUIC, and the edge's single QUIC listener.",
        ),
        ansible=(
            AnsibleContribution(
                feature="http3",
                roles_path=ansible.ROLES_PATH,
                edge_roles=(ansible.EDGE_ROLE,),
            ),
        ),
        nginx=(
            NginxContribution(
                feature="http3",
                templates_path=NGINX_TEMPLATES,
                server_fragments=("http3-server.conf.j2",),
                upstream_fragments=("http3-upstream.conf.j2",),
            ),
        ),
    ),
)
