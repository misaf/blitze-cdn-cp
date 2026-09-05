"""How well the cache is working, read back from the edges.

The outcome vocabulary is here rather than in `purge`, because it is nginx's
`$upstream_cache_status` — what the edges logged — and nothing about a purge
produces or consumes it.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "CACHE_CONSULTED_OUTCOMES",
    "CACHE_HIT_OUTCOMES",
    "CacheStatsReport",
    "EdgeStats",
    "SiteCacheStats",
]


#: Cache outcomes nginx records in ``$upstream_cache_status`` that mean the
#: response came from cache. EXPIRED and BYPASS went to the origin; REVALIDATED
#: did too, but only to confirm the stored copy, so it is counted as a hit —
#: the origin sent no body and the edge served what it already had.
CACHE_HIT_OUTCOMES = frozenset({"HIT", "STALE", "UPDATING", "REVALIDATED"})

#: Outcomes that consulted the cache at all. A request logging an empty value
#: never reached the cache — a redirect, an nginx-generated error, or a site
#: with caching disabled — and is excluded from the ratio rather than counted
#: as a miss.
CACHE_CONSULTED_OUTCOMES = CACHE_HIT_OUTCOMES | {"MISS", "EXPIRED", "BYPASS"}


class SiteCacheStats(BaseModel):
    """Cache outcomes for one virtual host on one edge."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    site: str
    outcomes: dict[str, int] = Field(default_factory=dict)

    @property
    def requests(self) -> int:
        """Every logged request, including those that never used the cache."""
        return sum(self.outcomes.values())

    @property
    def cacheable_requests(self) -> int:
        return sum(
            count
            for outcome, count in self.outcomes.items()
            if outcome in CACHE_CONSULTED_OUTCOMES
        )

    @property
    def hits(self) -> int:
        return sum(
            count
            for outcome, count in self.outcomes.items()
            if outcome in CACHE_HIT_OUTCOMES
        )

    @property
    def hit_ratio(self) -> float | None:
        """Hits over requests that consulted the cache, or None if none did.

        None rather than 0.0 on purpose: a site with no cacheable traffic has
        no hit ratio, and reporting zero would make an idle site look like a
        broken one on a dashboard.
        """
        total = self.cacheable_requests
        return round(self.hits / total, 4) if total else None


class EdgeStats(BaseModel):
    """One edge's report: what it served, and what nginx itself counted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str
    collected_at: datetime | None = None
    nginx_reachable: bool = False
    #: stub_status counters, cumulative since nginx started. Deltas need two
    #: readings, which this does not attempt to store.
    connections: dict[str, int] = Field(default_factory=dict)
    sites: tuple[SiteCacheStats, ...] = ()
    #: Set when the edge produced no usable report at all.
    error: str | None = None

    @property
    def hits(self) -> int:
        return sum(site.hits for site in self.sites)

    @property
    def cacheable_requests(self) -> int:
        return sum(site.cacheable_requests for site in self.sites)

    @property
    def requests(self) -> int:
        return sum(site.requests for site in self.sites)

    @property
    def hit_ratio(self) -> float | None:
        total = self.cacheable_requests
        return round(self.hits / total, 4) if total else None


class CacheStatsReport(BaseModel):
    """Cache effectiveness across the fleet, as of one collection run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    collected_at: datetime
    host_limit: str | None = None
    edges: tuple[EdgeStats, ...] = ()

    @property
    def reporting(self) -> tuple[EdgeStats, ...]:
        return tuple(edge for edge in self.edges if edge.error is None)

    @property
    def silent(self) -> tuple[EdgeStats, ...]:
        return tuple(edge for edge in self.edges if edge.error is not None)

    @property
    def hits(self) -> int:
        return sum(edge.hits for edge in self.reporting)

    @property
    def cacheable_requests(self) -> int:
        return sum(edge.cacheable_requests for edge in self.reporting)

    @property
    def requests(self) -> int:
        return sum(edge.requests for edge in self.reporting)

    @property
    def hit_ratio(self) -> float | None:
        """Fleet hit ratio, weighted by request volume rather than by edge.

        Averaging the per-edge ratios would let an edge serving a hundred
        requests move the number as much as one serving a million.
        """
        total = self.cacheable_requests
        return round(self.hits / total, 4) if total else None

    def by_site(self) -> tuple[SiteCacheStats, ...]:
        """The same numbers summed across edges, which is how a site is judged.

        A single edge's ratio for a site says as much about which clients
        landed there as about the cache.
        """
        merged: dict[str, dict[str, int]] = {}
        for edge in self.reporting:
            for site in edge.sites:
                outcomes = merged.setdefault(site.site, {})
                for outcome, count in site.outcomes.items():
                    outcomes[outcome] = outcomes.get(outcome, 0) + count
        return tuple(
            SiteCacheStats(site=name, outcomes=outcomes)
            for name, outcomes in sorted(merged.items())
        )
