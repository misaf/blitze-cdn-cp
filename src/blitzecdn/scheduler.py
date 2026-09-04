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

from blitzecdn.core.config import Settings
from blitzecdn.core.plugins import ScheduledJob
from blitzecdn.core.runtime.broker import enqueue_scheduled_once


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
        # copy behind it.
        #
        # The job says. This used to be computed here as the deployment
        # timeout plus the *certificate renewal budget* — core sizing every
        # job's lease from one optional capability's setting, on the reasoning
        # that certificate renewal is the slowest thing a job does. Two
        # problems, and the second is why removing it changes nothing: core
        # cannot read a detached capability's setting, and that floor never
        # bound anyway, because twice the interval exceeded it for every job
        # that has ever existed. `ScheduledJob.lease_seconds` was already
        # declared for exactly this and had never been read.
        lease = job.lease_seconds or job.interval_seconds * 2
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
