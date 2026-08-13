"""APScheduler registration for lightweight Dramatiq triggers."""

from __future__ import annotations

from datetime import UTC

from apscheduler.schedulers.background import (  # type: ignore[import-untyped]
    BackgroundScheduler,
)

from blitzecdn.config import Settings
from blitzecdn.infrastructure.queue import (
    check_drift,
    reconcile_certificates,
    renew_certificates,
)


def build_scheduler(settings: Settings) -> BackgroundScheduler | None:
    """Build scheduled queue triggers, or ``None`` when all are disabled."""
    scheduler = BackgroundScheduler(
        timezone=UTC,
        job_defaults={"coalesce": True, "max_instances": 1},
    )
    jobs = (
        (
            "certificate-reconciliation",
            settings.certificate_reconcile_interval_seconds,
            reconcile_certificates.send,
            0,
        ),
        (
            "certificate-renewal",
            settings.certificate_renewal_interval_seconds,
            renew_certificates.send,
            min(3600, settings.certificate_renewal_interval_seconds // 10),
        ),
        (
            "drift-check",
            settings.drift_check_interval_seconds,
            check_drift.send,
            min(600, settings.drift_check_interval_seconds // 10),
        ),
    )
    for job_id, interval, trigger, jitter in jobs:
        if interval:
            scheduler.add_job(
                trigger,
                "interval",
                id=job_id,
                seconds=interval,
                jitter=jitter or None,
            )
    return scheduler if scheduler.get_jobs() else None
