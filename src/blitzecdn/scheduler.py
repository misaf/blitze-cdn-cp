"""APScheduler registration for lightweight Dramatiq triggers."""

from __future__ import annotations

from datetime import UTC
from functools import partial

from apscheduler.schedulers.background import (  # type: ignore[import-untyped]
    BackgroundScheduler,
)

from blitzecdn.core.broker import enqueue_scheduled_once
from blitzecdn.core.config import Settings


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
            "reconcile-certificates",
            0,
        ),
        (
            "certificate-renewal",
            settings.certificate_renewal_interval_seconds,
            "renew-certificates",
            min(3600, settings.certificate_renewal_interval_seconds // 10),
        ),
        (
            "automatic-ssl-scan",
            settings.ssl_automatic_scan_interval_seconds,
            "reconcile-automatic-ssl",
            min(86_400, settings.ssl_automatic_scan_interval_seconds // 30),
        ),
        (
            "drift-check",
            settings.drift_check_interval_seconds,
            "check-drift",
            min(600, settings.drift_check_interval_seconds // 10),
        ),
    )
    for job_id, interval, operation, jitter in jobs:
        if interval:
            ttl = max(
                interval * 2,
                settings.deployment_timeout_seconds
                + settings.certificate_renewal_budget_seconds,
            )
            scheduler.add_job(
                partial(
                    enqueue_scheduled_once,
                    str(settings.redis_url),
                    operation,
                    ttl_seconds=ttl,
                ),
                "interval",
                id=job_id,
                seconds=interval,
                jitter=jitter or None,
            )
    return scheduler if scheduler.get_jobs() else None
