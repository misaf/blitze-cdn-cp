"""When recurring work is next due, kept where a restart cannot forget it.

The scheduler this replaces was an APScheduler timer in the API process. It
worked, and it had one property that only shows up on a bad day: the schedule
lived in memory. A controller that was down over the hour a certificate renewal
was due came back with the timer reset and simply never ran that firing — the
job's next chance was a whole interval later, and nothing anywhere recorded
that one had been missed.

A row fixes that by making "when is this next due" durable. A controller that
starts up and finds a due schedule enqueues it immediately, which is the
behaviour an operator already assumes they have.

Deliberately not cron. Every recurring job in this control plane is an interval
— renew what is expiring, check for drift, prune history — and none of them
cares what time of day it happens. A cron expression would be a parser, a
timezone question and a class of "why did this not fire" bugs, bought for a
scheduling precision nothing here has asked for.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["Schedule", "next_due"]


class Schedule(BaseModel):
    """One recurring job, and when it is next allowed to be enqueued."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: The scheduled job's name, which is also the plugin-unique identifier the
    #: worker resolves against the registry in its own process.
    name: str
    interval_seconds: int = Field(ge=1)
    jitter_seconds: int = Field(default=0, ge=0)
    #: How long a claim on this job's work is good for. A run that has not
    #: finished by then is stuck rather than slow, and the lease expiring is
    #: what lets another worker take it over.
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
    """When a schedule that just fired should fire again.

    From ``now`` rather than from the previous due time, which is the choice
    that matters after an outage: a controller that was down for six hours and
    comes back to a schedule six hours overdue runs it once and then resumes
    its cadence, instead of running it once per missed interval to catch up.
    Nothing here benefits from catching up — the work is "renew what is
    expiring", not "produce a report for each hour".

    ``entropy`` is a parameter so a test can pin the jitter. It defaults to the
    module-level generator, which is fine for spreading load and is not being
    asked for anything else.
    """
    spread = (entropy or random).uniform(0, jitter_seconds) if jitter_seconds else 0.0
    return now + timedelta(seconds=interval_seconds + spread)
