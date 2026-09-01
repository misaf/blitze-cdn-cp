"""Register the cache capability from a separately installable distribution.

The reference optional plugin, and deliberately dull: two routers, two command
groups, the Ansible roles this distribution ships, and the name to attribute
them to. Everything the capability actually *does* is in ``service.py``, reached
by an explicit call on a service this package builds for itself in
``composition.py`` — nothing about purging a cache goes through a hook.

Nothing in ``blitzecdn`` imports this module. The ``blitzecdn.plugins`` entry
point in this distribution's metadata is the whole of how it is found, so
installing the package makes the routes and commands appear and removing it
makes them go away, with no line of core edited either way.
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter

from blitzecdn.core.plugins import (
    AnsibleContribution,
    CliCommandGroup,
    PluginMetadata,
    hookimpl,
)
from blitzecdn_cache import ansible, cli
from blitzecdn_cache.api import v1, v2
from blitzecdn_cache.composition import __version__


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="cache",
        version=__version__,
        summary="Purge cached responses and read cache effectiveness.",
    )


@hookimpl
def blitzecdn_api_routers() -> Sequence[APIRouter]:
    return (v1.router, v2.router)


@hookimpl
def blitzecdn_ansible_contributions() -> Sequence[AnsibleContribution]:
    return (AnsibleContribution(plugin="cache", roles_path=ansible.ROLES_PATH),)


@hookimpl
def blitzecdn_cli_commands() -> Sequence[CliCommandGroup]:
    return (
        CliCommandGroup(name="cache", app=cli.cache_app),
        CliCommandGroup(name=None, app=cli.stats_app),
    )
