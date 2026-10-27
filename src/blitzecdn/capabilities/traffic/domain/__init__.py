"""Hourly visitor traffic and durable log progress."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from blitzecdn.core.domain.validation import hostname

Counter = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class TrafficValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HourlyUsage(TrafficValue):
    hostname: str
    hour: datetime
    requests: Annotated[Counter, Field(ge=1)]
    bytes_sent: Counter
    body_bytes_sent: Counter

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

    @model_validator(mode="after")
    def distinct_streams(self) -> Self:
        ids = [batch.end.stream_id for batch in self.batches]
        if len(ids) != len(set(ids)):
            raise ValueError("a collection may contain only one batch per stream")
        return self


class EdgeCollection(TrafficValue):
    edge: str
    requests: Counter = 0
    bytes_sent: Counter = 0
    errors: tuple[str, ...] = ()
    more: bool = False


class TrafficUsage(HourlyUsage):
    edge: str


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
        return HourlyUsage.utc_hour(value)

    @field_validator("hostname")
    @classmethod
    def valid_hostname(cls, value: str | None) -> str | None:
        return hostname(value) if value is not None else None

    @model_validator(mode="after")
    def ordered_range(self) -> Self:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self
