"""Declare the required durable-workflow capability."""

from __future__ import annotations

from blitzecdn import __version__
from blitzecdn.capabilities.workflows import cli
from blitzecdn.capabilities.workflows.api import routes
from blitzecdn.core.features import CliCommandGroup, Feature, FeatureMetadata

FEATURES = (
    Feature(
        metadata=FeatureMetadata(
            name="workflows",
            version=__version__,
            summary="Durable progress for work that crosses out of a transaction.",
        ),
        api_routers=(routes.router,),
        cli_commands=(
            CliCommandGroup(feature="workflows", name="workflow", app=cli.workflow_app),
        ),
    ),
)
