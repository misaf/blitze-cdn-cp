"""Per-site analytics over stored hourly traffic: queries, buckets, ownership.

Nothing here reads logs. Traffic accounting stores hourly counters per edge
and actual requested hostname; this module decides which hostnames a site
owns and judges the summed counters.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from blitzecdn.capabilities.cache.domain.statistics import (
    CACHE_CONSULTED_OUTCOMES,
    CACHE_HIT_OUTCOMES,
)
from blitzecdn.capabilities.traffic.domain import SiteHour, StatusClass
from blitzecdn.core.domain.site import CdnSite

__all__ = [
    "AnalyticsBucket",
    "Granularity",
    "PathsQuery",
    "SiteQuery",
    "owned_hostnames",
    "owner",
]


class AnalyticsValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Granularity(StrEnum):
    HOUR = "hour"
    DAY = "day"

    @property
    def step(self) -> timedelta:
        return timedelta(hours=1) if self is Granularity.HOUR else timedelta(days=1)


def _utc_hour(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("bounds require a timezone")
    value = value.astimezone(UTC)
    if value.minute or value.second or value.microsecond:
        raise ValueError("bounds must be exact UTC hours")
    return value


class RangedQuery(AnalyticsValue):
    site: Annotated[str, Field(min_length=1, max_length=253)]
    start: datetime
    end: datetime
    limit: Annotated[int, Field(ge=1, le=500)] = 100
    offset: Annotated[int, Field(ge=0, le=2**63 - 1)] = 0

    @field_validator("start", "end")
    @classmethod
    def utc_hour(cls, value: datetime) -> datetime:
        return _utc_hour(value)

    @model_validator(mode="after")
    def ordered_range(self) -> Self:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class SiteQuery(RangedQuery):
    """A dense series of buckets: every hour or UTC day, empty ones included."""

    granularity: Granularity = Granularity.HOUR

    @model_validator(mode="after")
    def whole_buckets(self) -> Self:
        if self.granularity is Granularity.DAY and (self.start.hour or self.end.hour):
            raise ValueError("daily buckets require bounds at UTC midnight")
        return self

    @property
    def buckets(self) -> int:
        return (self.end - self.start) // self.granularity.step

    def window(self) -> tuple[datetime, datetime] | None:
        """The page's bucket range, or None when the offset is past the end."""
        if self.offset >= self.buckets:
            return None
        step = self.granularity.step
        first = self.start + step * self.offset
        return first, min(self.end, first + step * self.limit)


class PathsQuery(RangedQuery):
    """Top paths over the whole range, most requested first."""


def _ratio(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


class AnalyticsBucket(AnalyticsValue):
    """One site's traffic in one hour or UTC day, summed over its hostnames."""

    start: datetime
    requests: int = 0
    bytes_sent: int = 0
    body_bytes_sent: int = 0
    #: Requests that consulted the cache, and those it answered.
    cacheable_requests: int = 0
    hits: int = 0
    hit_ratio: float | None = None
    #: Every class appears, with zero when there were none.
    status: dict[StatusClass, int] = Field(
        default_factory=lambda: dict.fromkeys(StatusClass, 0)
    )
    #: 4xx and 5xx over all requests; null for an empty bucket.
    client_error_ratio: float | None = None
    server_error_ratio: float | None = None

    @classmethod
    def of(cls, start: datetime, hours: Iterable[SiteHour]) -> AnalyticsBucket:
        requests = sent = body = consulted = hits = 0
        status = dict.fromkeys(StatusClass, 0)
        for hour in hours:
            requests += hour.requests
            sent += hour.bytes_sent
            body += hour.body_bytes_sent
            for outcome, counters in hour.cache.items():
                if outcome in CACHE_CONSULTED_OUTCOMES:
                    consulted += counters.requests
                if outcome in CACHE_HIT_OUTCOMES:
                    hits += counters.requests
            for status_class, count in hour.status.items():
                status[status_class] += count
        return cls(
            start=start,
            requests=requests,
            bytes_sent=sent,
            body_bytes_sent=body,
            cacheable_requests=consulted,
            hits=hits,
            hit_ratio=_ratio(hits, consulted),
            status=status,
            client_error_ratio=_ratio(status[StatusClass.CLIENT_ERROR], requests),
            server_error_ratio=_ratio(status[StatusClass.SERVER_ERROR], requests),
        )


def owner(hostname: str, sites: Iterable[CdnSite]) -> str | None:
    """The site nginx would serve ``hostname`` from, by server_name precedence.

    An exact name wins; otherwise the longest matching leading wildcard. Sites
    only allow exact names and ``*.`` wildcards, so no other forms apply.
    """
    best: tuple[int, str] | None = None
    for site in sites:
        for name in site.server_names:
            if name == hostname:
                return site.name
            suffix = name[1:] if name.startswith("*.") else None
            if (
                suffix is not None
                and hostname.endswith(suffix)
                and (best is None or len(suffix) > best[0])
            ):
                best = (len(suffix), site.name)
    return best[1] if best else None


def owned_hostnames(
    site: CdnSite, sites: Iterable[CdnSite], seen: Iterable[str]
) -> frozenset[str]:
    """The site's exact names plus every seen hostname its wildcards win."""
    every = tuple(sites)
    exact = {name for name in site.server_names if not name.startswith("*.")}
    return frozenset(exact) | frozenset(
        hostname
        for hostname in seen
        if hostname not in exact and owner(hostname, every) == site.name
    )
