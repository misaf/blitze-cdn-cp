"""HTTP representations owned by cache operations. Shared response behavior
comes from the API model toolkit; cache report shapes remain beside cache."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from pydantic import Field, field_validator

from blitzecdn.api.models import HostRun, Model
from blitzecdn.api.requests import FleetRequest
from blitzecdn.capabilities.cache.domain import PurgeEntry as DomainPurgeEntry
from blitzecdn.capabilities.http.policy import HttpScheme


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


class PurgeResult(Model):
    purged_at: datetime
    entries: tuple[PurgeEntry, ...] = ()
    purge_all: bool = False
    host_limit: str | None = None
    hosts: tuple[HostRun, ...] = ()
    complete: bool
    failed_hosts: tuple[str, ...]


class SiteCacheStats(Model):
    site: str
    outcomes: dict[str, int] = Field(default_factory=dict)


class EdgeStats(Model):
    host: str
    collected_at: datetime | None = None
    nginx_reachable: bool = False
    connections: dict[str, int] = Field(default_factory=dict)
    sites: tuple[SiteCacheStats, ...] = ()
    error: str | None = None


class CacheStatsReport(Model):
    collected_at: datetime
    host_limit: str | None = None
    edges: tuple[EdgeStats, ...] = ()


class PurgeRequest(FleetRequest):
    entries: list[PurgeEntry] = Field(default_factory=list, max_length=500)
    #: Empty the cache instead of removing named entries. Kept as its own flag
    #: rather than "no entries means everything" so a caller whose filter
    #: matched nothing cannot empty the fleet's cache by accident.
    purge_all: bool = False


class StatsRequest(FleetRequest):
    pass


__all__ = [
    "CacheStatsReport",
    "EdgeStats",
    "PurgeEntry",
    "PurgeRequest",
    "PurgeResult",
    "SiteCacheStats",
    "StatsRequest",
]
