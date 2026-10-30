"""Purging cached responses, and reading how well the cache is working.

Neither changes desired state or takes the deployment lock: a purge is most
needed exactly when a deploy is running.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from blitzecdn.capabilities.cache.contract import CachePolicy, CacheQueryStringMode
from blitzecdn.capabilities.cache.domain import (
    CacheStatsReport,
    EdgeStats,
    PurgeEntry,
    PurgeResult,
    SiteCacheStats,
)
from blitzecdn.capabilities.cache.ports import CacheRunner
from blitzecdn.capabilities.dns.ports import SiteReader
from blitzecdn.capabilities.http.contract import HttpScheme
from blitzecdn.capabilities.serving.domain import CdnSite
from blitzecdn.core.domain.events import domain_event
from blitzecdn.core.domain.runs import HostRun
from blitzecdn.core.exceptions import ConflictError, ExecutionError, NotFoundError
from blitzecdn.core.ports.clock import Clock, system_clock
from blitzecdn.core.ports.operations import EventRecorder


class CacheService:
    """What is cached on the edges, and getting rid of it."""

    def __init__(
        self,
        *,
        sites: SiteReader,
        events: EventRecorder,
        runner: CacheRunner,
        clock: Clock = system_clock,
    ) -> None:
        #: Read to decide which hostnames may be purged. This service never
        #: writes a site; purging is not a change to desired state.
        self.sites = sites
        self.events = events
        self.runner = runner
        self.clock = clock

    def purge_cache(
        self,
        operator: str,
        *,
        entries: Sequence[PurgeEntry] = (),
        purge_all: bool = False,
        host_limit: str | None = None,
    ) -> PurgeResult:
        """Remove cached responses from the edges, refusing hostnames no site serves.

        A purge that silently matches nothing looks the same as one that worked.
        """
        resolved_entries = self.check_purge(entries=entries, purge_all=purge_all)
        run = self.runner.run_cache_purge(
            entries=resolved_entries,
            purge_all=purge_all,
            host_limit=host_limit,
        )
        report = PurgeResult(
            purged_at=self.clock(),
            entries=resolved_entries,
            purge_all=purge_all,
            host_limit=host_limit,
            hosts=run.hosts,
        )
        self.events.record(
            domain_event(
                operator,
                "cache.purged",
                "site",
                None,
                {
                    "purge_all": purge_all,
                    "entries": [
                        entry.model_dump(mode="json") for entry in resolved_entries
                    ],
                    "host_limit": host_limit,
                    "complete": report.complete,
                    "failed": [host.host for host in report.failed],
                },
            )
        )
        if not report.hosts:
            raise ExecutionError(run.unreported("purge"))
        return report

    def check_purge(
        self, *, entries: Sequence[PurgeEntry] = (), purge_all: bool = False
    ) -> tuple[PurgeEntry, ...]:
        """Refuse a purge that could only report success having removed nothing.

        Returns the entries as the edges will key them. Run before queueing a
        purge, so a bad request fails when it is made.
        """
        if purge_all and entries:
            raise ConflictError(
                "purge everything and purge named entries are different "
                "operations; ask for one or the other"
            )
        if not purge_all and not entries:
            raise ConflictError(
                "nothing to purge: give at least one entry or purge all"
            )
        return self._resolve_purge_entries(entries) if entries else ()

    def _resolve_purge_entries(
        self, entries: Sequence[PurgeEntry]
    ) -> tuple[PurgeEntry, ...]:
        """Refuse impossible entries and reproduce each owning site's cache key.

        The hostname must match a site, wildcards as nginx matches them. The scheme
        must be one the site serves, since ``$scheme`` leads the key. Under ``ignore``
        query mode the query is stripped. Otherwise the purge hashes a key nginx never
        wrote and reports success.
        """
        exact: dict[str, CdnSite] = {}
        wildcards: list[tuple[str, CdnSite]] = []
        for site in self.sites.list_sites():
            if not site.policy.enabled:
                continue
            for name in site.server_names:
                if name.startswith("*."):
                    wildcards.append((name[2:], site))
                else:
                    exact.setdefault(name, site)

        unknown: set[str] = set()
        mismatched: set[str] = set()
        resolved: list[PurgeEntry] = []
        for entry in entries:
            owner = exact.get(entry.host) or next(
                (
                    candidate
                    for suffix, candidate in wildcards
                    if entry.host.endswith(f".{suffix}")
                ),
                None,
            )
            if owner is None:
                unknown.add(entry.host)
                continue
            served = HttpScheme.HTTPS if owner.serves_tls else HttpScheme.HTTP
            if entry.scheme is not served:
                mismatched.add(
                    f"{entry.scheme.value}://{entry.host}{entry.uri} "
                    f"({owner.name} serves {served.value})"
                )
                continue
            uri = entry.uri
            if (
                owner.policy[CachePolicy].cache_query_string_mode
                is CacheQueryStringMode.IGNORE
            ):
                uri = uri.partition("?")[0]
            resolved.append(PurgeEntry(host=entry.host, uri=uri, scheme=entry.scheme))
        if unknown:
            raise NotFoundError(
                "no enabled site serves: "
                + ", ".join(sorted(unknown))
                + ". A purge for a hostname nothing serves would report success "
                "having removed nothing."
            )
        if mismatched:
            raise ConflictError(
                "nothing is cached under the scheme requested for: "
                + "; ".join(sorted(mismatched))
                + ". The cache key starts with the scheme, so purging the other "
                "one would report success having removed nothing."
            )
        return tuple(resolved)

    def cache_stats(
        self, operator: str, *, host_limit: str | None = None
    ) -> CacheStatsReport:
        """Collect cache effectiveness from the edges, read-only and unlocked.

        Counters arrive on each ``HostRun.report``, so an edge that ran but reported
        nothing shows as silent rather than vanishing.
        """
        run = self.runner.run_stats(host_limit=host_limit)
        report = CacheStatsReport(
            collected_at=self.clock(),
            host_limit=host_limit,
            edges=tuple(_edge_stats(host) for host in run.hosts),
        )
        self.events.record(
            domain_event(
                operator,
                "cache.stats_collected",
                "deployment",
                None,
                {
                    "host_limit": host_limit,
                    "reporting": [edge.host for edge in report.reporting],
                    "silent": [edge.host for edge in report.silent],
                    "hit_ratio": report.hit_ratio,
                },
            )
        )
        if not report.edges:
            raise ExecutionError(run.unreported("statistics"))
        return report


def _edge_stats(host: HostRun) -> EdgeStats:
    """Read one edge's report defensively; a malformed one means "nothing usable"."""
    if not host.reached:
        return EdgeStats(host=host.host, error="unreachable")
    if not host.succeeded:
        return EdgeStats(host=host.host, error="the stats role failed on this edge")
    document = host.report
    if document is None:
        return EdgeStats(host=host.host, error="the edge published no report")
    outcomes: dict[str, dict[str, int]] = {}
    rows = document.get("cache")
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        site = str(row.get("site", "")).strip()
        outcome = str(row.get("outcome", "")).strip().upper()
        try:
            requests = int(row.get("requests", 0))
        except (TypeError, ValueError):
            continue
        if not site or not outcome or requests < 0:
            continue
        outcomes.setdefault(site, {})[outcome] = (
            outcomes.setdefault(site, {}).get(outcome, 0) + requests
        )
    raw_connections = document.get("connections")
    connections = {
        str(key): int(value)
        for key, value in (
            raw_connections.items() if isinstance(raw_connections, dict) else ()
        )
        if isinstance(value, (int, str)) and str(value).isdigit()
    }
    collected = document.get("collected_at")
    try:
        collected_at = datetime.fromisoformat(str(collected)) if collected else None
    except ValueError:
        collected_at = None
    return EdgeStats(
        host=host.host,
        collected_at=collected_at,
        nginx_reachable=bool(document.get("nginx_reachable")),
        connections=connections,
        sites=tuple(
            SiteCacheStats(site=name, outcomes=counts)
            for name, counts in sorted(outcomes.items())
        ),
    )
