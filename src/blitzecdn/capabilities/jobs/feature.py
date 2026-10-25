"""Declare the durable work queue: commands only.

No routes: clients ask about deployments and workflows, not the queue.
Schedules are registered by the composition root at process start.
"""

from __future__ import annotations

from blitzecdn import __version__
from blitzecdn.capabilities.jobs import cli
from blitzecdn.core.features import CliCommandGroup, Feature, FeatureMetadata

FEATURES = (
    Feature(
        metadata=FeatureMetadata(
            name="jobs",
            version=__version__,
            summary="Durable background work, on the control plane's own database.",
        ),
        cli_commands=(CliCommandGroup(feature="jobs", name="job", app=cli.job_app),),
    ),
)
