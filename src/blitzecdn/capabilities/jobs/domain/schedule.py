"""When recurring work is next due, kept where a restart cannot forget it.

Intervals, not cron: no job here cares about time of day.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["Schedule", "next_due"]


class Schedule(BaseModel):
    """One recurring job, and when it is next allowed to be enqueued."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: The scheduled job's name, which is also the feature-unique identifier the
    #: worker resolves against the registry in its own process.
    name: str
    interval_seconds: int = Field(ge=1)
    jitter_seconds: int = Field(default=0, ge=0)
    #: How long a claim on this job is good for before another worker takes over.
    lease_seconds: int = Field(ge=1)
    next_run_at: datetime
    last_run_at: datetime | None = None


def next_due(
    *,
    now: datetime,
    interval_seconds: int,
    jitter_seconds: int = 0,
    entropy: random.Random | None = None,
) -> datetime:
    """When a schedule that just fired should fire again, counted from ``now``.

    After an outage that runs it once, not once per missed interval. ``entropy``
    lets a test pin the jitter.
    """
    spread = (entropy or random).uniform(0, jitter_seconds) if jitter_seconds else 0.0
    return now + timedelta(seconds=interval_seconds + spread)
