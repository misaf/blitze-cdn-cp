"""The fleet roster: which edges exist, and how one is added or removed."""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter

from blitzecdn import __version__
from blitzecdn.core.plugins import CliCommandGroup, PluginMetadata, hookimpl
from blitzecdn.capabilities.edges import cli
from blitzecdn.capabilities.edges.api import routes


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="edges",
        version=__version__,
        required=True,
        summary="Register, update and decommission edge servers.",
    )


@hookimpl
def blitzecdn_api_routers() -> Sequence[APIRouter]:
    return (routes.router,)


@hookimpl
def blitzecdn_cli_commands() -> Sequence[CliCommandGroup]:
    # `origin` is not here any more: the group left with the play behind it,
    # into `blitzecdn-origins`, and appears only while that package is
    # installed.
    return (CliCommandGroup(name="edge", app=cli.edge_app),)
