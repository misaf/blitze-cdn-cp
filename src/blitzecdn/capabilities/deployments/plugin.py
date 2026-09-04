"""Convergence: the deployment lock, the runs, and the process lifecycle.

The startup contribution is why `RuntimeContext` carries a `ProcessKind`.
Republishing queued deployments and marking orphaned runs abandoned has to
happen while holding the fleet-wide deployment lock, and it has to happen once
per node — so it belongs to the API, which is the process that lives as long as
the node does. Doing it from every CLI invocation would take that lock on every
command an operator typed.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from fastapi import APIRouter

from blitzecdn import __version__
from blitzecdn.capabilities.deployments import cli
from blitzecdn.capabilities.deployments.api import routes
from blitzecdn.core.plugins import (
    CliCommandGroup,
    PluginMetadata,
    ProcessKind,
    RuntimeContext,
    ScheduledJob,
    hookimpl,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from blitzecdn.composition import ControlPlane


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="deployments",
        version=__version__,
        required=True,
        summary="Converge the fleet, roll back, and detect drift.",
    )


@hookimpl
def blitzecdn_api_routers() -> Sequence[APIRouter]:
    return (routes.router,)


@hookimpl
def blitzecdn_cli_commands() -> Sequence[CliCommandGroup]:
    return (CliCommandGroup(name=None, app=cli.deployment_app),)


@hookimpl
def blitzecdn_scheduled_jobs(platform: ControlPlane) -> Sequence[ScheduledJob]:
    interval = platform.settings.drift_check_interval_seconds

    def check(operator: str) -> None:
        platform.deployments.check_drift(operator)

    return (
        ScheduledJob(
            name="check-drift",
            interval_seconds=interval,
            run=check,
            jitter_seconds=min(600, interval // 10),
        ),
    )


@hookimpl
def blitzecdn_startup(context: RuntimeContext, platform: ControlPlane) -> None:
    """Republish queued deployments and abandon orphaned runs — API only."""
    if context.process is ProcessKind.API:
        platform.deployments.initialize()
