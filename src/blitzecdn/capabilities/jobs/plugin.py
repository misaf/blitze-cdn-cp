"""Register the durable work queue.

Contributes a command group and nothing else. There is no route: a job is
control-plane machinery rather than a resource a client manages, and the two
things a client actually wants to know — did my deployment run, did my
certificate renew — are already answerable from the deployment and workflow
endpoints. Publishing the queue would be publishing an implementation.

Required, because a control plane that failed to register this would accept a
deployment, record it as queued, and never run it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from blitzecdn import __version__
from blitzecdn.capabilities.jobs import cli
from blitzecdn.capabilities.jobs.composition import schedule_specs
from blitzecdn.core.plugins import (
    CliCommandGroup,
    PluginMetadata,
    ProcessKind,
    RuntimeContext,
    hookimpl,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from blitzecdn.composition import ControlPlane


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="jobs",
        version=__version__,
        api_version=1,
        required=True,
        summary="Durable background work, on the control plane's own database.",
    )


@hookimpl
def blitzecdn_cli_commands() -> Sequence[CliCommandGroup]:
    return (CliCommandGroup(plugin="jobs", name="job", app=cli.job_app),)


@hookimpl
def blitzecdn_startup(context: RuntimeContext, platform: ControlPlane) -> None:
    """Make the schedule table say what this installation contributes.

    The long-lived processes only. A CLI invocation is over in a second and has
    no business rewriting the fleet's schedule — and doing it from every command
    an operator typed would make ``blitzecdn domain list`` a writer.

    Both of the long-lived ones, though, and that is deliberate: the worker is
    what consumes a due schedule, but an API that came up first should already
    have the table an operator can read, and registration is idempotent and
    never moves an existing schedule's next firing. A schedule no installed
    plugin contributes any more is dropped here too, so an uninstalled package
    stops producing one "no such job" per interval.
    """
    if context.process in (ProcessKind.API, ProcessKind.WORKER):
        platform.job_scheduler.register(schedule_specs(platform.jobs))
