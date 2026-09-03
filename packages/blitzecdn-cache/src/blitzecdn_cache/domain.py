"""Purging cached responses, and reading how well the cache is working."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from blitzecdn.capabilities.http.policy import HttpScheme
from blitzecdn.core.runs import HostRun
from blitzecdn.core.validation import hostname


class PurgeEntry(BaseModel):
    """One cached response to remove, named the way a client would request it.

    Deliberately a hostname and a path rather than a site name: the cache is
    keyed by the ``Host`` header nginx saw, and a site can answer to several
    hostnames. Purging "the site" would have to purge every one of them and
    still could not express "only the apex".
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str
    uri: str
    #: The scheme a *client* used, not the one the edge fetches with. It leads
    #: the cache key, so it selects between two genuinely different entries;
    #: ``EdgeOperationsService`` refuses one the site could not have stored.
    scheme: HttpScheme = HttpScheme.HTTPS

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        return hostname(value)

    @field_validator("uri")
    @classmethod
    def validate_uri(cls, value: str) -> str:
        """Accept the request target nginx would have logged, and nothing else.

        No path normalization beyond stripping: cache keys preserve the raw
        path, so ``/a/./b`` and ``/a/b`` are genuinely different entries.
        ``CacheService`` removes only the query when the owning site explicitly
        uses ignore-query mode.
        """
        candidate = value.strip()
        if not candidate.startswith("/"):
            raise ValueError("uri must be an absolute path beginning with '/'")
        if len(candidate) > 2048:
            raise ValueError("uri must be at most 2048 characters")
        if any(character.isspace() for character in candidate):
            raise ValueError("uri cannot contain whitespace")
        return candidate


class PurgeResult(BaseModel):
    """Which edges carried out a purge, and which did not.

    ``succeeded`` counts hosts that ran the removal, not entries deleted: nginx
    open source cannot report whether a key was present, so "deleted 3 files"
    and "there was nothing to delete" are the same observation. Treat a
    successful purge as "this object is not being served from cache any more",
    which is the question actually being asked, rather than as proof it was
    there.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    purged_at: datetime
    entries: tuple[PurgeEntry, ...] = ()
    purge_all: bool = False
    host_limit: str | None = None
    hosts: tuple[HostRun, ...] = ()

    @property
    def succeeded(self) -> tuple[HostRun, ...]:
        return tuple(host for host in self.hosts if host.succeeded)

    @property
    def failed(self) -> tuple[HostRun, ...]:
        return tuple(host for host in self.hosts if not host.succeeded)

    #: Serialised, not just computed. A partial purge is reported with the
    #: whole result rather than as a bare error string, so a client scripting a
    #: retry can read which edges still serve the object instead of parsing
    #: prose out of a ``detail`` field. That makes ``complete`` the flag the
    #: client branches on, which means it has to be in the body.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def complete(self) -> bool:
        """True only if every edge in scope purged.

        A partial purge is the dangerous outcome: the object is gone from some
        edges and still served by others, so which response a client gets
        depends on which edge answers.
        """
        return bool(self.hosts) and not self.failed

    @computed_field  # type: ignore[prop-decorator]
    @property
    def failed_hosts(self) -> tuple[str, ...]:
        """The edges that may still be serving the cached copy, by name.

        Derivable from ``hosts``, but named here because it is the one field an
        operator acts on and it should not require walking a per-host result to
        find.
        """
        return tuple(host.host for host in self.failed)


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
