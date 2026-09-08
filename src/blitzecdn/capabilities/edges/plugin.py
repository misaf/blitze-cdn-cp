"""The fleet roster: which edges exist, and how one is added or removed."""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter

from blitzecdn import __version__
from blitzecdn.capabilities.edges import cli
from blitzecdn.capabilities.edges.api import routes
from blitzecdn.core.plugins import CliCommandGroup, PluginMetadata, hookimpl


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="edges",
        version=__version__,
        api_version=1,
        required=True,
        summary="Register, update and decommission edge servers.",
    )


@hookimpl
def blitzecdn_api_routers() -> Sequence[APIRouter]:
    return (routes.router,)


@hookimpl
def blitzecdn_cli_commands() -> Sequence[CliCommandGroup]:
    # `origin` is not here: the group travels with the play behind it, in
    # `blitzecdn-origins`, and appears only while that package is installed.
    return (CliCommandGroup(plugin="edges", name="edge", app=cli.edge_app),)
