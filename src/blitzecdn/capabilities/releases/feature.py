"""The compiler: what the fleet should be serving, as an addressable value.

Contributes routes and commands only; it assembles what other capabilities
contribute and adds no variables of its own.
"""

from __future__ import annotations

from blitzecdn import __version__
from blitzecdn.capabilities.releases import cli
from blitzecdn.capabilities.releases.api import routes
from blitzecdn.core.features import CliCommandGroup, Feature, FeatureMetadata

FEATURES = (
    Feature(
        metadata=FeatureMetadata(
            name="releases",
            version=__version__,
            summary="Compile desired state into immutable, addressable releases.",
        ),
        api_routers=(routes.router,),
        cli_commands=(
            CliCommandGroup(feature="releases", name="release", app=cli.release_app),
        ),
    ),
)
