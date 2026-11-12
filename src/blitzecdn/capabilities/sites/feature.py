"""The site catalogue: zones, policy, rules, records, and the hosts they derive."""

from __future__ import annotations

from blitzecdn import __version__
from blitzecdn.capabilities.sites import cli
from blitzecdn.capabilities.sites.api import routes
from blitzecdn.core.features import (
    CliCommandGroup,
    Feature,
    FeatureMetadata,
)

FEATURES = (
    Feature(
        metadata=FeatureMetadata(
            name="sites",
            version=__version__,
            summary="Zones, policy, rules and records, and the hosts they derive.",
        ),
        api_routers=(routes.router,),
        cli_commands=(
            CliCommandGroup(
                feature="sites",
                name="domain",
                app=cli.domain_app,
                order=cli.DOMAIN_ORDER,
            ),
            CliCommandGroup(feature="sites", name="record", app=cli.record_app),
            CliCommandGroup(feature="sites", name="rule", app=cli.rule_app),
        ),
    ),
)
