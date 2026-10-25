"""Declare the application's cache capability."""

from __future__ import annotations

from blitzecdn.capabilities.cache import ansible, cli
from blitzecdn.capabilities.cache.api import routes
from blitzecdn.capabilities.cache.wiring import __version__
from blitzecdn.core.features import (
    AnsibleContribution,
    CliCommandGroup,
    Feature,
    FeatureMetadata,
    NginxContribution,
)
from blitzecdn.core.runtime.resources import package_directory

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
            name="cache",
            version=__version__,
            provides=frozenset({"cache"}),
            summary="Purge cached responses and read cache effectiveness.",
        ),
        api_routers=(routes.router,),
        # Two groups, because the command tree is an interface decision: `purge`
        # nests under `cache`, while `stats` is a verb an operator types
        # directly and stays a root command.
        cli_commands=(
            CliCommandGroup(feature="cache", name="cache", app=cli.cache_app),
            CliCommandGroup(feature="cache", name=None, app=cli.stats_app),
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
    ),
)
