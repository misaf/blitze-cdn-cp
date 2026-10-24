"""Declare the required durable-workflow capability."""

from __future__ import annotations

from blitzecdn import __version__
from blitzecdn.capabilities.workflows.api import routes
from blitzecdn.core.plugins import Feature, PluginMetadata

FEATURES = (
    Feature(
        metadata=PluginMetadata(
            name="workflows",
            version=__version__,
            api_version=2,
            required=True,
            summary="Durable progress for work that crosses out of a transaction.",
        ),
        api_routers=(routes.router,),
    ),
)
