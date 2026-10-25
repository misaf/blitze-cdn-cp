"""The compiler: what the fleet should be serving, as an addressable value.

Contributes routes and commands and nothing else — no Ansible role, no
desired-state variables, no edge module. That is the shape of a capability
which *assembles* what the others contribute rather than contributing anything
of its own: a reader looking for "where does the compiler put its own
variables into the document" should find here that it has none.

Required, like `dns` and `edges`. A control plane without it would have no way
to answer what an edge should serve, so it would not be a degraded control
plane but a wrong one.
"""

from __future__ import annotations

from blitzecdn import __version__
from blitzecdn.capabilities.releases import cli
from blitzecdn.capabilities.releases.api import routes
from blitzecdn.core.plugins import CliCommandGroup, Feature, PluginMetadata

FEATURES = (
    Feature(
        metadata=PluginMetadata(
            name="releases",
            version=__version__,
            summary="Compile desired state into immutable, addressable releases.",
        ),
        api_routers=(routes.router,),
        cli_commands=(
            CliCommandGroup(plugin="releases", name="release", app=cli.release_app),
        ),
    ),
)
