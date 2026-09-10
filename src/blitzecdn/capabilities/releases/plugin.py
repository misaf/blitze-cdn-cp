"""The compiler: what the fleet should be serving, as an addressable value.

Contributes routes and commands and nothing else — no Ansible role, no
desired-state variables, no edge module. That is the shape of a capability
which *assembles* what the others contribute rather than contributing anything
of its own, and it is worth stating plainly: a reader looking for "where does
the compiler put its own variables into the document" should find here that it
has none.

Required, like `dns` and `edges`. A control plane that failed to register this
would have no way to answer what an edge should serve, so continuing without it
would not be a degraded control plane but a wrong one.
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter

from blitzecdn import __version__
from blitzecdn.capabilities.releases import cli
from blitzecdn.capabilities.releases.api import routes
from blitzecdn.core.plugins import CliCommandGroup, PluginMetadata, hookimpl


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="releases",
        version=__version__,
        api_version=1,
        required=True,
        summary="Compile desired state into immutable, addressable releases.",
    )


@hookimpl
def blitzecdn_api_routers() -> Sequence[APIRouter]:
    return (routes.router,)


@hookimpl
def blitzecdn_cli_commands() -> Sequence[CliCommandGroup]:
    return (CliCommandGroup(plugin="releases", name="release", app=cli.release_app),)
