"""Per-site questions answered from the stored hourly aggregates.

A site is one served hostname: the key traffic is stored under. Every figure
here is a sum of hourly rows across edges; nothing reads raw logs.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, computed_field, field_validator, model_validator

from blitzecdn.capabilities.traffic.domain import (
    CACHE_CONSULTED_OUTCOMES,
    CACHE_HIT_OUTCOMES,
    CacheOutcome,
    HourlyUsage,
    StatusClass,
    TrafficValue,
)
from blitzecdn.core.domain.validation import hostname

__all__ = [
    "Granularity",
    "OutcomeTotals",
    "PathTotals",
    "SiteBucket",
    "SiteQuery",
]


class Granularity(StrEnum):
    HOUR = "hour"
    DAY = "day"


class SiteQuery(TrafficValue):
    """One site over ``[start, end)``, optionally on one edge."""

    site: str
    start: datetime
    end: datetime
    edge: Annotated[str | None, Field(min_length=1, max_length=253)] = None
    granularity: Granularity = Granularity.HOUR
    limit: Annotated[int, Field(ge=1, le=500)] = 100
    offset: Annotated[int, Field(ge=0, le=2**63 - 1)] = 0

    @field_validator("site")
    @classmethod
    def valid_site(cls, value: str) -> str:
        return hostname(value)

    @field_validator("start", "end")
    @classmethod
    def utc_hour(cls, value: datetime) -> datetime:
        return HourlyUsage.utc_hour(value)

    @model_validator(mode="after")
    def ordered_range(self) -> Self:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        if self.granularity is Granularity.DAY and (self.start.hour or self.end.hour):
            raise ValueError("daily buckets need start and end at UTC midnight")
        return self


def _ratio(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


class OutcomeTotals(TrafficValue):
    requests: int
    bytes_sent: int


class SiteBucket(TrafficValue):
    """One site's totals for one UTC hour or day, summed across edges."""

    start: datetime
    requests: int
    bytes_sent: int
    body_bytes_sent: int
    cache: dict[CacheOutcome, OutcomeTotals] = Field(default_factory=dict)
    status: dict[StatusClass, int] = Field(default_factory=dict)

    def _cache_sum(self, among: frozenset[CacheOutcome]) -> int:
        return sum(
            item.requests for outcome, item in self.cache.items() if outcome in among
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def hits(self) -> int:
        return self._cache_sum(CACHE_HIT_OUTCOMES)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cacheable_requests(self) -> int:
        return self._cache_sum(CACHE_CONSULTED_OUTCOMES)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def hit_ratio(self) -> float | None:
        """Hits over cache-consulting requests; None when none consulted it."""
        return _ratio(self.hits, self.cacheable_requests)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def error_ratio(self) -> float | None:
        """5xx over requests with a recorded status class; None for none."""
        return _ratio(
            self.status.get(StatusClass.SERVER_ERROR, 0), sum(self.status.values())
        )


class PathTotals(TrafficValue):
    """A path's requests summed over the hours where it was among the busiest."""

    path: str
    requests: int
    bytes_sent: int
