"""APScheduler registration for the jobs the installed plugins contribute.

This module knows how *often* to ask and how to publish the ask. It does not
know what any job does, or even which jobs exist: they arrive as
:class:`~blitzecdn.core.plugins.ScheduledJob` values from the plugin registry,
so a separately installed package can add recurring work without a line here
changing.

Each trigger publishes the job's name to the queue behind a single-flight key,
and a worker resolves that name against the same registry in its own process.
The scheduler never runs the work itself: it is in the API process, which must
stay able to answer requests while a renewal is running somewhere else.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC
from functools import partial

from apscheduler.schedulers.background import (  # type: ignore[import-untyped]
    BackgroundScheduler,
)

from blitzecdn.core.broker import enqueue_scheduled_once
from blitzecdn.core.config import Settings
from blitzecdn.core.plugins import ScheduledJob


def build_scheduler(
    settings: Settings, jobs: Mapping[str, ScheduledJob]
) -> BackgroundScheduler | None:
    """Build triggers for every enabled job, or ``None`` when none is enabled."""
    scheduler = BackgroundScheduler(
        timezone=UTC,
        job_defaults={"coalesce": True, "max_instances": 1},
    )
    for name, job in sorted(jobs.items()):
        if not job.interval_seconds:
            continue
        # How long the single-flight key is held: long enough that a trigger
        # firing while the previous run is still going does not stack a second
        # copy behind it, which for the slowest job means a whole convergence
        # plus the renewal budget it may spend before starting one.
        lease = max(
            job.interval_seconds * 2,
            settings.deployment_timeout_seconds
            + settings.certificate_renewal_budget_seconds,
        )
        scheduler.add_job(
            partial(
                enqueue_scheduled_once,
                str(settings.redis_url),
                name,
                ttl_seconds=lease,
            ),
            "interval",
            id=name,
            seconds=job.interval_seconds,
            jitter=job.jitter_seconds or None,
        )
    return scheduler if scheduler.get_jobs() else None
