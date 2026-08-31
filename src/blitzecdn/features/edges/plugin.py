"""The fleet roster: which edges exist, and whether their origins answer."""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter

from blitzecdn import __version__
from blitzecdn.core.plugins import CliCommandGroup, PluginMetadata, hookimpl
from blitzecdn.features.edges import cli
from blitzecdn.features.edges.api import v1, v2


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="edges",
        version=__version__,
        required=True,
        summary="Register edge servers and check their origins.",
    )


@hookimpl
def blitzecdn_api_routers() -> Sequence[APIRouter]:
    return (v1.router, v2.router)


@hookimpl
def blitzecdn_cli_commands() -> Sequence[CliCommandGroup]:
    return (
        CliCommandGroup(name="edge", app=cli.edge_app),
        CliCommandGroup(name="origin", app=cli.origin_app),
    )
