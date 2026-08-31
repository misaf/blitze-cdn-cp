"""The one scheduled job that belongs to no single feature.

Reconciliation issues certificates and then, if anything was issued, re-runs the
automatic-SSL scan — a site that has just gained a certificate may now qualify
for an upgrade it did not qualify for a minute ago. That sequencing is a
decision about two features and is therefore not either feature's to make,
which is why this small package exists at all.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from blitzecdn import __version__
from blitzecdn.core.plugins import PluginMetadata, ScheduledJob, hookimpl

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from blitzecdn.bootstrap import ControlPlane


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="maintenance",
        version=__version__,
        required=True,
        summary="Scheduled reconciliation across the certificate features.",
    )


@hookimpl
def blitzecdn_scheduled_jobs(platform: ControlPlane) -> Sequence[ScheduledJob]:
    def reconcile(operator: str) -> None:
        if platform.certificates.reconcile_certificates(operator).issued:
            platform.automatic_ssl.reconcile(operator)

    return (
        ScheduledJob(
            name="certificate-reconciliation",
            interval_seconds=platform.settings.certificate_reconcile_interval_seconds,
            run=reconcile,
        ),
    )
