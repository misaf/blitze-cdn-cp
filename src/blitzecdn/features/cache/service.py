"""Purging cached responses, and reading how well the cache is working.

Both operations run a playbook across the edges and neither changes desired
state: no deployment record is written and the deployment lock is deliberately
not taken, because the moment a purge is most needed is the moment a deploy is
most likely to already be running.

Separate from :mod:`blitzecdn.features.edges.service` despite also acting on the
fleet. That service answers "which hosts exist"; this one answers "what is
stored on them", which is a different question with its own domain module
(:mod:`blitzecdn.features.cache.domain`) and its own entry-point module. They were one
class only because both reach the edges, which is a statement about the
transport rather than about the work.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from blitzecdn.core.events import domain_event
from blitzecdn.core.exceptions import ConflictError, ExecutionError, NotFoundError
from blitzecdn.core.operation_ports import EventRecorder
from blitzecdn.core.runs import HostRun
from blitzecdn.features.cache.domain import (
    CacheStatsReport,
    EdgeStats,
    PurgeEntry,
    PurgeResult,
    SiteCacheStats,
)
from blitzecdn.features.cache.ports import CacheRunner
from blitzecdn.features.dns.ports import SiteStore
from blitzecdn.features.sites.domain import CdnSite
from blitzecdn.features.sites.policy import CacheQueryStringMode, HttpScheme


class CacheService:
    """What is cached on the edges, and getting rid of it."""

    def __init__(
        self,
        *,
        sites: SiteStore,
        events: EventRecorder,
        runner: CacheRunner,
    ) -> None:
        #: Read to decide which hostnames may be purged. This service never
        #: writes a site; purging is not a change to desired state.
        self.sites = sites
        self.events = events
        self.runner = runner

    def purge_cache(
        self,
        operator: str,
        *,
        entries: Sequence[PurgeEntry] = (),
        purge_all: bool = False,
        host_limit: str | None = None,
    ) -> PurgeResult:
        """Remove cached responses from the edges.

        Which hostnames may be purged is decided here rather than on the edge:
        a hostname no site serves is refused, because a purge that quietly
        matches nothing is indistinguishable from one that worked, and the
        operator learns nothing until the stale object is still being served.

        Purging is not deploying. Nothing about desired state changes, no
        deployment record is written, and the deployment lock is not taken —
        the moment a purge is most needed is the moment a deploy is most likely
        to already be running.
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
        resolved_entries = self._resolve_purge_entries(entries) if entries else ()
        run = self.runner.run_cache_purge(
            entries=resolved_entries,
            purge_all=purge_all,
            host_limit=host_limit,
        )
        report = PurgeResult(
            purged_at=datetime.now(UTC),
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

    def _resolve_purge_entries(
        self, entries: Sequence[PurgeEntry]
    ) -> tuple[PurgeEntry, ...]:
        """Refuse impossible entries and reproduce each owning site's cache URI.

        Two ways that happens, and either would otherwise report a successful
        purge of nothing.

        The hostname may be one no site answers to. Wildcard server names match
        their subdomains, the same way nginx matches them, so purging
        ``a.cdn.example.com`` under a ``*.example.com`` site is allowed.

        Or the scheme may be one the site never serves. The cache key begins
        with ``$scheme``, so the two are different entries: a site with TLS
        answers port 80 with a 301 and caches nothing under ``http``, and a site
        without TLS never sees an ``https`` request at all. Purging the wrong one
        computes a different MD5, deletes a file that was never written, and
        reports every edge as purged.
        A site whose query-string mode is ``ignore`` caches the raw path without
        its query. Strip it here too; otherwise the purge role hashes a key
        nginx never wrote and reports success while leaving the object live.
        """
        exact: dict[str, CdnSite] = {}
        wildcards: list[tuple[str, CdnSite]] = []
        for site in self.sites.list_sites():
            if not site.enabled:
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
            if owner.cache_query_string_mode is CacheQueryStringMode.IGNORE:
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
        """Collect cache effectiveness from the edges.

        Read-only and unlocked. Every edge's counters arrive on its own
        ``HostRun.report``, published by the role as the ``blitzecdn_report``
        fact, so the roster and the numbers are the same object — an edge that
        ran but produced nothing is reported as silent rather than vanishing
        from the fleet, and there is no controller-side directory to reset,
        collide over, or read a previous run's document out of.
        """
        run = self.runner.run_stats(host_limit=host_limit)
        report = CacheStatsReport(
            collected_at=datetime.now(UTC),
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
    """Read one edge's published report defensively.

    The shape is ours — `blitzecdn_stats` builds it — but it crossed a machine
    boundary, so a partial or malformed document must degrade to "this edge
    said nothing usable" rather than raise out of a fleet-wide report.
    """
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
