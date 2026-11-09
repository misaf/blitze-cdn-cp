"""Declare Automatic SSL/TLS: the origin scan and its upgrade-only writes."""

from __future__ import annotations

from blitzecdn.capabilities.automatic_ssl import __version__, cli
from blitzecdn.capabilities.automatic_ssl.api import routes
from blitzecdn.capabilities.automatic_ssl.settings import CONFIGURATION
from blitzecdn.core.features import CliCommandGroup, Feature, FeatureMetadata

FEATURES = (
    Feature(
        metadata=FeatureMetadata(
            name="automatic_ssl",
            version=__version__,
            summary="Scan origins and raise sites to the strongest safe SSL mode.",
        ),
        api_routers=(routes.router,),
        cli_commands=(
            CliCommandGroup(feature="automatic_ssl", name="ssl", app=cli.ssl_app),
        ),
        configuration=(CONFIGURATION,),
    ),
)
