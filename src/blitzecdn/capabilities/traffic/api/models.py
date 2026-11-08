"""Published hourly usage counters."""

from datetime import datetime

from blitzecdn.capabilities.traffic.domain import (
    CacheOutcome,
    FreshnessState,
    StatusClass,
)
from blitzecdn.entrypoints.api.models import Model


class CacheCounters(Model):
    requests: int
    bytes_sent: int


class TrafficUsage(Model):
    edge: str
    hostname: str
    hour: datetime
    requests: int
    bytes_sent: int
    body_bytes_sent: int
    #: The hour's requests split by cache outcome; NONE never consulted the cache.
    cache: dict[CacheOutcome, CacheCounters]
    #: Requests by response status class; classes with none are omitted.
    status: dict[StatusClass, int]


class SiteBucket(Model):
    """One site's totals for one UTC hour or day, summed across edges."""

    #: The bucket's first UTC hour: the hour itself, or midnight of the day.
    start: datetime
    requests: int
    bytes_sent: int
    body_bytes_sent: int
    #: Requests and bytes by cache outcome; NONE never consulted the cache.
    cache: dict[CacheOutcome, CacheCounters]
    #: Requests by response status class; classes with none are omitted.
    status: dict[StatusClass, int]
    #: HIT, STALE, UPDATING and REVALIDATED.
    hits: int
    #: Requests that consulted the cache: every outcome except NONE.
    cacheable_requests: int
    #: hits / cacheable_requests; null when nothing consulted the cache.
    hit_ratio: float | None
    #: 5xx / requests with a status class; null when none had one.
    error_ratio: float | None


class SitePath(Model):
    """A path's requests over the range, counted in hours it was among the
    busiest; a lower bound."""

    path: str
    requests: int
    bytes_sent: int


class CollectionStatus(Model):
    edge: str
    registered: bool
    collection_enabled: bool
    interval_seconds: int
    stale_after_seconds: int
    checked_at: datetime
    state: FreshnessState
    stale: bool
    last_attempt_started_at: datetime | None = None
    last_attempt_finished_at: datetime | None = None
    last_success_at: datetime | None = None
    last_caught_up_at: datetime | None = None
    pending: bool | None = None
    last_error: str | None = None
