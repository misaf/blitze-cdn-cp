"""Published per-site analytics."""

from __future__ import annotations

from datetime import datetime

from blitzecdn.capabilities.traffic.domain import StatusClass
from blitzecdn.entrypoints.api.models import Model

__all__ = ["AnalyticsBucket", "PathCounters"]


class AnalyticsBucket(Model):
    """One site's traffic in one hour or UTC day, over every edge."""

    #: The bucket's first UTC hour; it covers one granularity step.
    start: datetime
    requests: int
    bytes_sent: int
    body_bytes_sent: int
    #: Requests that consulted the cache: every outcome except NONE.
    cacheable_requests: int
    #: HIT, STALE, UPDATING and REVALIDATED.
    hits: int
    #: hits / cacheable_requests; null when nothing consulted the cache.
    hit_ratio: float | None
    #: Requests per status class; every class is present.
    status: dict[StatusClass, int]
    #: 4xx / requests and 5xx / requests; null for an empty bucket.
    client_error_ratio: float | None
    server_error_ratio: float | None


class PathCounters(Model):
    #: The request path without its query string.
    path: str
    requests: int
    bytes_sent: int
