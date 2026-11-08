"""Answer per-site questions from stored hourly traffic."""

from __future__ import annotations

from datetime import datetime

from blitzecdn.capabilities.analytics.domain import (
    AnalyticsBucket,
    PathsQuery,
    SiteQuery,
    owned_hostnames,
)
from blitzecdn.capabilities.analytics.ports import StoredTraffic
from blitzecdn.capabilities.dns.ports import SiteReader
from blitzecdn.capabilities.traffic.domain import PathCounters, RangeQuery, SiteHour
from blitzecdn.core.domain.pagination import PageResult

__all__ = ["AnalyticsService"]


class AnalyticsService:
    def __init__(self, *, sites: SiteReader, traffic: StoredTraffic) -> None:
        self._sites = sites
        self._traffic = traffic

    def series(self, query: SiteQuery) -> PageResult[AnalyticsBucket]:
        """One page of a dense bucket series; ``total`` counts every bucket."""
        window = query.window()
        if window is None:
            self._sites.get_site(query.site)  # Unknown sites are still 404.
            return PageResult(items=[], total=query.buckets)
        start, end = window
        step = query.granularity.step
        grouped: dict[int, list[SiteHour]] = {}
        for hour in self._traffic.site_hours(self._range(query.site, start, end)):
            grouped.setdefault((hour.hour - start) // step, []).append(hour)
        count = (end - start) // step
        return PageResult(
            items=[
                AnalyticsBucket.of(start + step * index, grouped.get(index, ()))
                for index in range(count)
            ],
            total=query.buckets,
        )

    def top_paths(self, query: PathsQuery) -> PageResult[PathCounters]:
        return self._traffic.top_paths(
            self._range(query.site, query.start, query.end),
            limit=query.limit,
            offset=query.offset,
        )

    def _range(self, name: str, start: datetime, end: datetime) -> RangeQuery:
        """The hostnames ``name`` owns in this range, resolved with current config.

        Wildcard sites own whichever seen hostnames nginx would route to them,
        so those are looked up; exact names need no scan.
        """
        site = self._sites.get_site(name)
        wildcard = any(item.startswith("*.") for item in site.server_names)
        seen = self._traffic.hostnames(start=start, end=end) if wildcard else ()
        return RangeQuery(
            hostnames=owned_hostnames(site, self._sites.list_sites(), seen),
            start=start,
            end=end,
        )
