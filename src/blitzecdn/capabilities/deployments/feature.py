"""Declare convergence: the deployment lock, the runs, and drift detection.

Startup recovery and the drift check are operations, built from the
deployment service in :mod:`blitzecdn.capabilities.deployments.runtime`.
"""

from __future__ import annotations

from blitzecdn import __version__
from blitzecdn.capabilities.deployments import cli
from blitzecdn.capabilities.deployments.api import routes
from blitzecdn.core.features import CliCommandGroup, Feature, FeatureMetadata

FEATURES = (
    Feature(
        metadata=FeatureMetadata(
            name="deployments",
            version=__version__,
            summary="Converge the fleet, roll back, and detect drift.",
        ),
        api_routers=(routes.router,),
        cli_commands=(
            CliCommandGroup(feature="deployments", name=None, app=cli.deployment_app),
        ),
    ),
)
