"""The cache operations' HTTP requests. Reports are the queued operation's result."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from pydantic import Field, field_validator

from blitzecdn.capabilities.cache.domain import PurgeEntry as DomainPurgeEntry
from blitzecdn.capabilities.cache.domain import PurgePrefix as DomainPurgePrefix
from blitzecdn.capabilities.cache.domain import PurgeTag
from blitzecdn.capabilities.http.contract import HttpScheme
from blitzecdn.entrypoints.api.models import Model
from blitzecdn.entrypoints.api.requests import FleetRequest


class PurgeEntry(Model):
    host: str
    uri: str
    scheme: HttpScheme = HttpScheme.HTTPS

    @field_validator("host", "uri")
    @classmethod
    def valid_entry_field(cls, value: str, info: Any) -> str:
        payload = {"host": "example.com", "uri": "/", info.field_name: value}
        return cast(
            "str", getattr(DomainPurgeEntry.model_validate(payload), info.field_name)
        )

    def to_domain(self) -> DomainPurgeEntry:
        return DomainPurgeEntry.model_validate(self.model_dump())


class PurgePrefix(Model):
    """Everything cached for ``host`` whose request target starts with ``prefix``.

    Leave ``prefix`` at ``/`` to purge the whole hostname. Both schemes and
    every port, method and encoding match.
    """

    host: str
    prefix: str = "/"

    @field_validator("host", "prefix")
    @classmethod
    def valid_prefix_field(cls, value: str, info: Any) -> str:
        payload = {"host": "example.com", "prefix": "/", info.field_name: value}
        return cast(
            "str", getattr(DomainPurgePrefix.model_validate(payload), info.field_name)
        )

    def to_domain(self) -> DomainPurgePrefix:
        return DomainPurgePrefix.model_validate(self.model_dump())


class PurgeRequest(FleetRequest):
    entries: list[PurgeEntry] = Field(default_factory=list, max_length=500)
    #: Each one is a pass over every cached file on every edge, so few are allowed.
    prefixes: list[PurgePrefix] = Field(default_factory=list, max_length=50)
    #: Every cached response whose ``Cache-Tag`` header names one of these, on
    #: any hostname. Case-insensitive. Matched in the same pass over every
    #: cached file on every edge as the prefixes, so as few are allowed.
    tags: list[PurgeTag] = Field(default_factory=list, max_length=50)
    #: A separate flag, so a filter matching nothing never empties the cache.
    purge_all: bool = False


class ConnectionsRequest(FleetRequest):
    pass


class OutcomeCounters(Model):
    requests: int
    bytes_sent: int


class CacheHour(Model):
    """One hostname's stored cache outcomes on one edge in one UTC hour."""

    edge: str
    hostname: str
    hour: datetime
    #: Every logged request, including NONE (never consulted the cache).
    requests: int
    #: Requests that consulted the cache: every outcome except NONE.
    cacheable_requests: int
    #: HIT, STALE, UPDATING and REVALIDATED.
    hits: int
    #: hits / cacheable_requests; null when nothing consulted the cache.
    hit_ratio: float | None
    bytes_sent: int
    #: Bytes sent for hits: what the origin did not have to send.
    cached_bytes_sent: int
    #: cached_bytes_sent / bytes of cache-consulting requests; null for none.
    byte_hit_ratio: float | None
    outcomes: dict[str, OutcomeCounters]


__all__ = [
    "CacheHour",
    "ConnectionsRequest",
    "OutcomeCounters",
    "PurgeEntry",
    "PurgePrefix",
    "PurgeRequest",
]
