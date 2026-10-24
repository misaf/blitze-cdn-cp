"""The fleet roster: which edges exist, and how one is added or removed."""

from __future__ import annotations

from blitzecdn import __version__
from blitzecdn.capabilities.edges import cli
from blitzecdn.capabilities.edges.api import routes
from blitzecdn.core.plugins import CliCommandGroup, Feature, PluginMetadata

FEATURES = (
    Feature(
        metadata=PluginMetadata(
            name="edges",
            version=__version__,
            api_version=2,
            required=True,
            summary="Register, update and decommission edge servers.",
        ),
        api_routers=(routes.router,),
        # `origin` is not here: the group travels with the play behind it, in
        # the `origins` feature.
        cli_commands=(CliCommandGroup(plugin="edges", name="edge", app=cli.edge_app),),
    ),
)
