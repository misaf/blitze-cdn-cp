"""Declare the durable work queue.

Contributes a command group and nothing else. There is no route: a job is
control-plane machinery rather than a resource a client manages, and the two
things a client actually wants to know — did my deployment run, did my
certificate renew — are already answerable from the deployment and workflow
endpoints. Publishing the queue would be publishing an implementation.

Registering the installation's schedules is not here either. It consumes every
feature's scheduled jobs at once, so the composition root does it when a
long-lived process starts.

Required, because a control plane without this would accept a deployment,
record it as queued, and never run it.
"""

from __future__ import annotations

from blitzecdn import __version__
from blitzecdn.capabilities.jobs import cli
from blitzecdn.core.plugins import CliCommandGroup, Feature, PluginMetadata

FEATURES = (
    Feature(
        metadata=PluginMetadata(
            name="jobs",
            version=__version__,
            api_version=2,
            required=True,
            summary="Durable background work, on the control plane's own database.",
        ),
        cli_commands=(CliCommandGroup(plugin="jobs", name="job", app=cli.job_app),),
    ),
)
