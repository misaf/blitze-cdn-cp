"""Register the required durable-workflow capability."""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter

from blitzecdn import __version__
from blitzecdn.capabilities.workflows.api import routes
from blitzecdn.core.plugins import PluginMetadata, hookimpl


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="workflows",
        version=__version__,
        required=True,
        summary="Durable progress for work that crosses out of a transaction.",
    )


@hookimpl
def blitzecdn_api_routers() -> Sequence[APIRouter]:
    return (routes.router,)
