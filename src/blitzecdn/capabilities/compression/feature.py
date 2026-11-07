"""Declare the application's compression capability."""

from __future__ import annotations

from blitzecdn.capabilities.compression import ansible, cli
from blitzecdn.capabilities.compression.contract import CompressionPolicy
from blitzecdn.core.features import (
    AnsibleContribution,
    CliCommandGroup,
    EdgeModule,
    Feature,
    FeatureMetadata,
    NginxContribution,
    PolicyContribution,
)
from blitzecdn.core.runtime.resources import distribution_version, package_directory

#: The installed distribution's version, read from package metadata.
__version__ = distribution_version(__name__)

#: The Nginx fragments this capability contributes, rendered into each
#: release on the controller rather than shipped inside a role.
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
            name="compression",
            version=__version__,
            provides=frozenset({"compression"}),
            summary="Which encodings a managed edge may produce: gzip and Brotli.",
        ),
        cli_commands=(
            CliCommandGroup(
                feature="compression",
                name="domain",
                app=cli.domain_commands,
                extends=True,
            ),
        ),
        ansible=(
            AnsibleContribution(
                feature="compression",
                roles_path=ansible.ROLES_PATH,
                edge_roles=(ansible.EDGE_ROLE,),
                # Brotli is not compiled into Nginx. Only the filter module: a proxy
                # cache has no pre-compressed `.br` files for the static one.
                edge_modules=(
                    EdgeModule(
                        name="brotli",
                        objects=("ngx_http_brotli_filter_module.so",),
                        probe="brotli off;",
                    ),
                ),
            ),
        ),
        nginx=(
            NginxContribution(
                feature="compression",
                templates_path=NGINX_TEMPLATES,
                server_fragments=("compression-server.conf.j2",),
            ),
        ),
        site_policy=(
            PolicyContribution(
                feature="compression", group="compression", contract=CompressionPolicy
            ),
        ),
    ),
)
