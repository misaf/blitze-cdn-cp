"""Declare the durable work queue.

One route: reading a queued edge operation. Deployment and scheduled jobs are
read through their own resources. Schedules are registered by the composition
root at process start.
"""

from __future__ import annotations

from blitzecdn import __version__
from blitzecdn.capabilities.jobs import cli
from blitzecdn.capabilities.jobs.api import routes
from blitzecdn.core.features import CliCommandGroup, Feature, FeatureMetadata

FEATURES = (
    Feature(
        metadata=FeatureMetadata(
            name="jobs",
            version=__version__,
            summary="Durable background work, on the control plane's own database.",
        ),
        api_routers=(routes.router,),
        cli_commands=(CliCommandGroup(feature="jobs", name="job", app=cli.job_app),),
    ),
)
