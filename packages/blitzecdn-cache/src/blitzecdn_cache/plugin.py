"""Register the cache capability from a separately installable distribution.

The reference optional plugin, and deliberately dull: two routers, two command
groups, the two Ansible roles this distribution ships, and the name to attribute
them to. Everything the capability actually *does* is in ``service.py``, reached
by an explicit call on a service this package builds for itself in
``composition.py`` — nothing about purging a cache goes through a hook.

Nothing in ``blitzecdn`` imports this module. The ``blitzecdn.plugins`` entry
point in this distribution's metadata is the whole of how it is found, so
installing the package makes the routes and the commands appear and removing it
makes them go away, with no line of core edited either way.
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter

from blitzecdn.core.plugins import (
    AnsibleContribution,
    CliCommandGroup,
    NginxContribution,
    PluginMetadata,
    hookimpl,
)
from blitzecdn.core.runtime.resources import package_directory
from blitzecdn_cache import ansible, cli
from blitzecdn_cache.api import routes
from blitzecdn_cache.composition import __version__

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
        required=False,
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
        CliCommandGroup(name="cache", app=cli.cache_app),
        CliCommandGroup(name=None, app=cli.stats_app),
    )
