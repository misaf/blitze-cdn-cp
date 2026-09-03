"""The HTTP representations this capability publishes, and the bodies it takes.

They live here rather than in ``blitzecdn.api.operations`` for the reason the
whole extraction exists: a detachable package's resource shapes are the
package's, and core cannot carry a ``PurgeResult`` for a capability that may
not be installed. What core still owns is the frame — ``OperationModel``, the
``as_operation`` projection and ``HostRun`` — which every capability's
operational representation is built from, this one included.

Identical in v1 and v2, and defined once for the same reason the operational
models in core are: every published version has always accepted and returned
the same shape, and a second class would rename the other version's schema.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from pydantic import Field, field_validator

from blitzecdn.api.operations import HostRun, OperationModel
from blitzecdn.api.requests import FleetRequest
from blitzecdn.capabilities.http.policy import HttpScheme
from blitzecdn_cache.domain import PurgeEntry as DomainPurgeEntry


class PurgeEntry(OperationModel):
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


class PurgeResult(OperationModel):
    purged_at: datetime
    entries: tuple[PurgeEntry, ...] = ()
    purge_all: bool = False
    host_limit: str | None = None
    hosts: tuple[HostRun, ...] = ()
    complete: bool
    failed_hosts: tuple[str, ...]


class SiteCacheStats(OperationModel):
    site: str
    outcomes: dict[str, int] = Field(default_factory=dict)


class EdgeStats(OperationModel):
    host: str
    collected_at: datetime | None = None
    nginx_reachable: bool = False
    connections: dict[str, int] = Field(default_factory=dict)
    sites: tuple[SiteCacheStats, ...] = ()
    error: str | None = None


class CacheStatsReport(OperationModel):
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
