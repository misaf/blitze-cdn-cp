"""Declare the application's cache capability."""

from __future__ import annotations

from blitzecdn.capabilities.cache import ansible, cli
from blitzecdn.capabilities.cache.api import routes
from blitzecdn.capabilities.cache.contract import CachePolicy
from blitzecdn.capabilities.cache.wiring import __version__
from blitzecdn.core.features import (
    AnsibleContribution,
    CliCommandGroup,
    Feature,
    FeatureMetadata,
    NginxContribution,
    PolicyContribution,
)
from blitzecdn.core.runtime.resources import package_directory

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
            name="cache",
            version=__version__,
            provides=frozenset({"cache"}),
            summary="Purge cached responses and read cache effectiveness.",
        ),
        api_routers=(routes.router,),
        # `cache purge` nests; `stats` is a root verb.
        cli_commands=(
            CliCommandGroup(feature="cache", name="cache", app=cli.cache_app),
            CliCommandGroup(feature="cache", name=None, app=cli.stats_app),
            CliCommandGroup(
                feature="cache",
                name="domain",
                app=cli.domain_commands,
                extends=True,
            ),
        ),
        ansible=(
            AnsibleContribution(
                feature="cache",
                roles_path=ansible.ROLES_PATH,
                edge_roles=(ansible.EDGE_ROLE,),
            ),
        ),
        nginx=(
            NginxContribution(
                feature="cache",
                templates_path=NGINX_TEMPLATES,
                http_fragments=("cache-http.conf.j2",),
                upstream_fragments=("cache-upstream.conf.j2",),
            ),
        ),
        site_policy=(
            PolicyContribution(feature="cache", group="cache", contract=CachePolicy),
        ),
    ),
)
