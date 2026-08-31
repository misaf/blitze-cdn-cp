"""Deciding, from what the edges observe, when a site can be upgraded to TLS."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from fastapi import APIRouter

from blitzecdn import __version__
from blitzecdn.core.plugins import PluginMetadata, ScheduledJob, hookimpl
from blitzecdn.features.automatic_ssl.api import v1, v2

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from blitzecdn.bootstrap import ControlPlane


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="automatic_ssl",
        version=__version__,
        required=True,
        summary="Upgrade origins to TLS once the edges confirm they answer.",
    )


@hookimpl
def blitzecdn_api_routers() -> Sequence[APIRouter]:
    return (v1.router, v2.router)


@hookimpl
def blitzecdn_scheduled_jobs(platform: ControlPlane) -> Sequence[ScheduledJob]:
    interval = platform.settings.ssl_automatic_scan_interval_seconds

    def scan(operator: str) -> None:
        # The reconciliation report is for the caller who asked for one; a
        # scheduled run has no caller, and the work it did is already in the
        # audit trail.
        platform.automatic_ssl.reconcile(operator)

    return (
        ScheduledJob(
            name="automatic-ssl-scan",
            interval_seconds=interval,
            run=scan,
            jitter_seconds=min(86_400, interval // 30),
        ),
    )
