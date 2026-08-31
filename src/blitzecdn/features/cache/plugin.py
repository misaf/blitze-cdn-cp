"""How the cache feature tells the control plane it exists.

The reference implementation of a feature plugin, and deliberately dull: two
routers, one command group, and the name to attribute them to. Everything the
feature actually *does* is in `service.py`, reached by an explicit call on a
constructor-injected `CacheService` — nothing about purging a cache goes
through a hook.
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter

from blitzecdn import __version__
from blitzecdn.core.plugins import CliCommandGroup, PluginMetadata, hookimpl
from blitzecdn.features.cache import cli
from blitzecdn.features.cache.api import v1, v2


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="cache",
        version=__version__,
        required=True,
        summary="Purge cached responses and read cache effectiveness.",
    )


@hookimpl
def blitzecdn_api_routers() -> Sequence[APIRouter]:
    return (v1.router, v2.router)


@hookimpl
def blitzecdn_cli_commands() -> Sequence[CliCommandGroup]:
    return (CliCommandGroup(name="cache", app=cli.cache_app),)
