"""Declare per-site analytics over stored traffic."""

from __future__ import annotations

from blitzecdn.capabilities.analytics import cli
from blitzecdn.capabilities.analytics.api import routes
from blitzecdn.capabilities.analytics.wiring import __version__
from blitzecdn.core.features import CliCommandGroup, Feature, FeatureMetadata

FEATURES = (
    Feature(
        metadata=FeatureMetadata(
            name="analytics",
            version=__version__,
            provides=frozenset({"analytics"}),
            summary="Read per-site requests, bandwidth, cache and status trends.",
        ),
        api_routers=(routes.router,),
        cli_commands=(
            CliCommandGroup(
                feature="analytics", name="analytics", app=cli.analytics_app
            ),
        ),
    ),
)
