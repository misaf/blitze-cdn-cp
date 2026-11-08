"""Hourly visitor traffic and durable log progress."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from blitzecdn.core.domain.validation import hostname

Counter = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class TrafficValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CacheOutcome(StrEnum):
    """Nginx's ``$upstream_cache_status``, with NONE for never consulted.

    Which outcomes count as hits is the cache capability's judgement; traffic
    only counts each record under the label it carried.
    """

    HIT = "HIT"
    STALE = "STALE"
    UPDATING = "UPDATING"
    REVALIDATED = "REVALIDATED"
    MISS = "MISS"
    EXPIRED = "EXPIRED"
    BYPASS = "BYPASS"
    NONE = "NONE"


class StatusClass(StrEnum):
    """An HTTP status code's class. Codes outside 100-599 are not classed."""

    INFORMATIONAL = "1xx"
    SUCCESS = "2xx"
    REDIRECTION = "3xx"
    CLIENT_ERROR = "4xx"
    SERVER_ERROR = "5xx"


#: Paths an edge reports per hostname and hour, and the controller keeps per
#: edge, hostname and hour. Ranks are decided at collection, not query time.
TOP_PATHS = 10
STORED_PATHS = 20


class CacheCounters(TrafficValue):
    requests: Annotated[Counter, Field(ge=1)]
    bytes_sent: Counter


class PathCounters(TrafficValue):
    """One request path's share of an hour; query strings never reach here."""

    #: Printable ASCII; ``?`` and ``#`` cannot appear, so neither can a query.
    path: Annotated[
        str, Field(min_length=1, max_length=512, pattern=r"^/[!-\"$->@-~]*$")
    ]
    requests: Annotated[Counter, Field(ge=1)]
    bytes_sent: Counter


class HourlyCounters(TrafficValue):
    hostname: str
    hour: datetime
    requests: Annotated[Counter, Field(ge=1)]
    bytes_sent: Counter
    body_bytes_sent: Counter
    #: The same records split by cache outcome; each record has exactly one.
    cache: Annotated[dict[CacheOutcome, CacheCounters], Field(min_length=1)]

    @model_validator(mode="after")
    def cache_partitions_totals(self) -> Self:
        if sum(item.requests for item in self.cache.values()) != self.requests or (
            sum(item.bytes_sent for item in self.cache.values()) != self.bytes_sent
        ):
            raise ValueError("cache outcomes must partition the hour's totals")
        return self

    @field_validator("hostname")
    @classmethod
    def valid_hostname(cls, value: str) -> str:
        return hostname(value)

    @field_validator("hour")
    @classmethod
    def utc_hour(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("hour requires a timezone")
        value = value.astimezone(UTC)
        if value.minute or value.second or value.microsecond:
            raise ValueError("hour must be an exact UTC hour")
        return value


class HourlyUsage(HourlyCounters):
    """One collected hour, with its status classes and its top paths.

    Status classes may sum to less than ``requests``: a status outside 100-599
    is counted as a request but not classed. Paths are the edge's top ranked
    paths for this batch, not a partition of the hour.
    """

    status: dict[StatusClass, Annotated[Counter, Field(ge=1)]] = Field(
        default_factory=dict
    )
    paths: Annotated[tuple[PathCounters, ...], Field(max_length=TOP_PATHS)] = ()

    @model_validator(mode="after")
    def breakdowns_within_totals(self) -> Self:
        if sum(self.status.values()) > self.requests:
            raise ValueError("status classes cannot exceed the hour's requests")
        if len({item.path for item in self.paths}) != len(self.paths):
            raise ValueError("paths must be unique")
        if any(
            item.requests > self.requests or item.bytes_sent > self.bytes_sent
            for item in self.paths
        ):
            raise ValueError("a path cannot exceed the hour's totals")
        return self


class LogCursor(TrafficValue):
    stream_id: Digest
    offset: Annotated[Counter, Field(ge=1)]
    anchor: Digest


class UsageBatch(TrafficValue):
    start: LogCursor | None = None
    end: LogCursor
    buckets: Annotated[tuple[HourlyUsage, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def advances_one_stream(self) -> Self:
        if self.start and (
            self.start.stream_id != self.end.stream_id
            or self.end.offset <= self.start.offset
        ):
            raise ValueError("a batch must advance its own stream")
        return self


class CollectedUsage(TrafficValue):
    batches: tuple[UsageBatch, ...]
    errors: tuple[str, ...] = ()
    more: bool = False
    missing_streams: Annotated[tuple[Digest, ...], Field(max_length=128)] = ()

    @model_validator(mode="after")
    def distinct_streams(self) -> Self:
        ids = [batch.end.stream_id for batch in self.batches]
        if len(ids) != len(set(ids)):
            raise ValueError("a collection may contain only one batch per stream")
        if self.missing_streams and (self.more or self.errors):
            raise ValueError("missing streams require a complete, error-free inventory")
        if set(ids) & set(self.missing_streams):
            raise ValueError("a reported batch cannot also be a missing stream")
        return self


class RetentionResult(TrafficValue):
    cutoff: datetime
    deleted_hours: int
    more: bool


class EdgeCollection(TrafficValue):
    edge: str
    requests: Counter = 0
    bytes_sent: Counter = 0
    errors: tuple[str, ...] = ()
    more: bool = False


class TrafficUsage(HourlyCounters):
    edge: str


class SiteHour(TrafficValue):
    """Several hostnames' counters on every edge, summed for one UTC hour."""

    hour: datetime
    requests: Counter
    bytes_sent: Counter
    body_bytes_sent: Counter
    cache: dict[CacheOutcome, CacheCounters] = Field(default_factory=dict)
    status: dict[StatusClass, Counter] = Field(default_factory=dict)


class RangeQuery(TrafficValue):
    """Exact hostnames over whole UTC hours, independent of any site model."""

    hostnames: Annotated[frozenset[str], Field(max_length=10_000)]
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def utc_hour(cls, value: datetime) -> datetime:
        return HourlyCounters.utc_hour(value)

    @model_validator(mode="after")
    def ordered_range(self) -> Self:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class UsageQuery(TrafficValue):
    start: datetime
    end: datetime
    hostname: str | None = None
    edge: Annotated[str | None, Field(min_length=1, max_length=253)] = None
    limit: Annotated[int, Field(ge=1, le=500)] = 100
    offset: Annotated[int, Field(ge=0, le=2**63 - 1)] = 0

    @field_validator("start", "end")
    @classmethod
    def utc_hour(cls, value: datetime) -> datetime:
        return HourlyCounters.utc_hour(value)

    @field_validator("hostname")
    @classmethod
    def valid_hostname(cls, value: str | None) -> str | None:
        return hostname(value) if value is not None else None

    @model_validator(mode="after")
    def ordered_range(self) -> Self:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class CollectionRecord(TrafficValue):
    edge: str
    started_at: datetime
    finished_at: datetime | None = None
    last_success_at: datetime | None = None
    last_caught_up_at: datetime | None = None
    pending: bool | None = None
    last_error: str | None = None


class FreshnessState(StrEnum):
    DISABLED = "disabled"
    NEVER_COLLECTED = "never_collected"
    COLLECTING = "collecting"
    FAILED = "failed"
    BACKLOGGED = "backlogged"
    STALE = "stale"
    CURRENT = "current"


class CollectionStatus(TrafficValue):
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
