"""What per-site analytics reads: stored traffic and the derived sites."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from blitzecdn.capabilities.traffic.domain import PathCounters, RangeQuery, SiteHour
from blitzecdn.core.domain.pagination import PageResult

__all__ = ["StoredTraffic"]


class StoredTraffic(Protocol):
    """Traffic accounting's saved hours; reading never contacts an edge."""

    def hostnames(self, *, start: datetime, end: datetime) -> frozenset[str]: ...

    def site_hours(self, query: RangeQuery) -> tuple[SiteHour, ...]: ...

    def top_paths(
        self, query: RangeQuery, *, limit: int, offset: int
    ) -> PageResult[PathCounters]: ...
