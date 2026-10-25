"""The fleet roster: which edges exist, and how one is added or removed."""

from __future__ import annotations

from blitzecdn import __version__
from blitzecdn.capabilities.edges import cli
from blitzecdn.capabilities.edges.api import routes
from blitzecdn.core.features import CliCommandGroup, Feature, FeatureMetadata

FEATURES = (
    Feature(
        metadata=FeatureMetadata(
            name="edges",
            version=__version__,
            summary="Register, update and decommission edge servers.",
        ),
        api_routers=(routes.router,),
        # `origin` is not here: the group travels with the play behind it, in
        # the `origins` feature.
        cli_commands=(CliCommandGroup(feature="edges", name="edge", app=cli.edge_app),),
    ),
)
