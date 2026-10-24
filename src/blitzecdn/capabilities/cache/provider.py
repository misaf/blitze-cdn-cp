"""Register the application's cache implementation.

All code and resources ship with BlitzeCDN. Registration contributes delivery,
configuration or concrete edge resources; business operations remain services.
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter

from blitzecdn.capabilities.cache import ansible, cli
from blitzecdn.capabilities.cache.api import routes
from blitzecdn.capabilities.cache.composition import __version__
from blitzecdn.core.plugins import (
    AnsibleContribution,
    CliCommandGroup,
    NginxContribution,
    PluginMetadata,
    hookimpl,
)
from blitzecdn.core.runtime.resources import package_directory

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
        name="cache",
        version=__version__,
        api_version=1,
        required=True,
        provides=frozenset({"cache"}),
        summary="Purge cached responses and read cache effectiveness.",
    )


@hookimpl
def blitzecdn_api_routers() -> Sequence[APIRouter]:
    return (routes.router,)


@hookimpl
def blitzecdn_ansible_contributions() -> Sequence[AnsibleContribution]:
    # The purge and statistics roles, from inside this wheel. Core adds the
    # directory to Ansible's role search path and never learns what is in it;
    # uninstalling this distribution removes both roles from every subsequent
    # run without a line of the control plane changing.
    return (
        AnsibleContribution(
            plugin="cache",
            roles_path=ansible.ROLES_PATH,
            edge_roles=(ansible.EDGE_ROLE,),
        ),
    )


@hookimpl
def blitzecdn_nginx_contributions() -> Sequence[NginxContribution]:
    return (
        NginxContribution(
            plugin="cache",
            templates_path=NGINX_TEMPLATES,
            http_fragments=("cache-http.conf.j2",),
            upstream_fragments=("cache-upstream.conf.j2",),
        ),
    )


@hookimpl
def blitzecdn_cli_commands() -> Sequence[CliCommandGroup]:
    # Two groups, because the command tree is an interface decision and not a
    # consequence of packaging: `purge` is a thing you do to the cache and
    # nests under it, while `stats` is a verb an operator types directly and
    # stays a root command, exactly as it read before this became a package.
    return (
        CliCommandGroup(plugin="cache", name="cache", app=cli.cache_app),
        CliCommandGroup(plugin="cache", name=None, app=cli.stats_app),
    )
