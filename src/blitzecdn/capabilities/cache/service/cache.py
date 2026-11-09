"""Purging cached responses, and reading how well the cache is working.

Neither changes desired state or takes the deployment lock: a purge is most
needed exactly when a deploy is running.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import NamedTuple

from blitzecdn.capabilities.cache.contract import CachePolicy, CacheQueryStringMode
from blitzecdn.capabilities.cache.domain import (
    CacheHour,
    ConnectionsReport,
    EdgeConnections,
    OutcomeCounters,
    PurgeEntry,
    PurgePrefix,
    PurgeResult,
    purge_tag,
)
from blitzecdn.capabilities.cache.ports import CacheRunner, CacheUsage
from blitzecdn.capabilities.dns import SiteReader
from blitzecdn.capabilities.tls.contract import TlsPolicy
from blitzecdn.capabilities.traffic import UsageQuery
from blitzecdn.core.domain.events import domain_event
from blitzecdn.core.domain.pagination import PageResult
from blitzecdn.core.domain.runs import HostRun
from blitzecdn.core.domain.site import CdnSite
from blitzecdn.core.exceptions import ConflictError, ExecutionError, NotFoundError
from blitzecdn.core.ports.clock import Clock
from blitzecdn.core.ports.operations import EventRecorder


class PurgeTargets(NamedTuple):
    """A purge request as the edges will match it, after the sites have vetted it."""

    entries: tuple[PurgeEntry, ...] = ()
    prefixes: tuple[PurgePrefix, ...] = ()
    tags: tuple[str, ...] = ()


#: Tags one purge may name. Each is matched in the same pass over every cached
#: file as the prefixes, and the API caps both alike.
MAX_PURGE_TAGS = 50


class CacheService:
    """What is cached on the edges, and getting rid of it."""

    def __init__(
        self,
        *,
        sites: SiteReader,
        events: EventRecorder,
        runner: CacheRunner,
        usage: CacheUsage,
        clock: Clock,
    ) -> None:
        #: Read to decide which hostnames may be purged. This service never
        #: writes a site; purging is not a change to desired state.
        self.sites = sites
        self.events = events
        self.runner = runner
        self.usage = usage
        self.clock = clock

    def purge_cache(
        self,
        operator: str,
        *,
        entries: Sequence[PurgeEntry] = (),
        prefixes: Sequence[PurgePrefix] = (),
        tags: Sequence[str] = (),
        purge_all: bool = False,
        host_limit: str | None = None,
    ) -> PurgeResult:
        """Remove cached responses from the edges, refusing hostnames no site serves.

        A purge that silently matches nothing looks the same as one that worked.
        """
        targets = self.check_purge(
            entries=entries, prefixes=prefixes, tags=tags, purge_all=purge_all
        )
        run = self.runner.run_cache_purge(
            entries=targets.entries,
            prefixes=targets.prefixes,
            tags=targets.tags,
            purge_all=purge_all,
            host_limit=host_limit,
        )
        report = PurgeResult(
            purged_at=self.clock(),
            entries=targets.entries,
            prefixes=targets.prefixes,
            tags=targets.tags,
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
                        entry.model_dump(mode="json") for entry in targets.entries
                    ],
                    "prefixes": [
                        prefix.model_dump(mode="json") for prefix in targets.prefixes
                    ],
                    "tags": list(targets.tags),
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
        self,
        *,
        entries: Sequence[PurgeEntry] = (),
        prefixes: Sequence[PurgePrefix] = (),
        tags: Sequence[str] = (),
        purge_all: bool = False,
    ) -> PurgeTargets:
        """Refuse a purge that could only report success having removed nothing.

        Returns the entries, prefixes and tags as the edges will match them.
        Run before queueing a purge, so a bad request fails when it is made.
        """
        if purge_all and (entries or prefixes or tags):
            raise ConflictError(
                "purge everything and purge named entries, prefixes or tags are "
                "different operations; ask for one or the other"
            )
        if not purge_all and not entries and not prefixes and not tags:
            raise ConflictError(
                "nothing to purge: give at least one entry, prefix or tag, or purge all"
            )
        owners = self._site_owners()
        return PurgeTargets(
            entries=self._resolve_purge_entries(entries, owners) if entries else (),
            prefixes=(
                self._resolve_purge_prefixes(prefixes, owners) if prefixes else ()
            ),
            tags=_resolve_purge_tags(tags),
        )

    def _site_owners(self) -> _SiteOwners:
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
        return _SiteOwners(exact, tuple(wildcards))

    def _resolve_purge_prefixes(
        self, prefixes: Sequence[PurgePrefix], owners: _SiteOwners
    ) -> tuple[PurgePrefix, ...]:
        """Refuse prefixes for hostnames no site serves, or that no key can start with.

        A site that ignores the query string keys by path alone, so a prefix
        reaching into the query would match nothing it stored.
        """
        unknown: set[str] = set()
        queried: set[str] = set()
        for prefix in prefixes:
            owner = owners.owner_of(prefix.host)
            if owner is None:
                unknown.add(prefix.host)
            elif "?" in prefix.prefix and (
                owner.policy[CachePolicy].cache_query_string_mode
                is CacheQueryStringMode.IGNORE
            ):
                queried.add(f"{prefix.host}{prefix.prefix} ({owner.name})")
        _refuse_unknown_hosts(unknown)
        if queried:
            raise ConflictError(
                "these sites cache by path and ignore the query string, so no "
                "cache key contains the query in: "
                + "; ".join(sorted(queried))
                + ". Drop the query from the prefix."
            )
        # Order kept, duplicates dropped: each prefix is a pass over the cache.
        return tuple(dict.fromkeys(prefixes))

    def _resolve_purge_entries(
        self, entries: Sequence[PurgeEntry], owners: _SiteOwners
    ) -> tuple[PurgeEntry, ...]:
        """Refuse impossible entries and reproduce each owning site's cache key.

        The hostname must match a site, wildcards as nginx matches them. The scheme
        must be one the site serves, since ``$scheme`` leads the key. Under ``ignore``
        query mode the query is stripped. Otherwise the purge hashes a key nginx never
        wrote and reports success.
        """
        unknown: set[str] = set()
        mismatched: set[str] = set()
        resolved: list[PurgeEntry] = []
        for entry in entries:
            owner = owners.owner_of(entry.host)
            if owner is None:
                unknown.add(entry.host)
                continue
            served = owner.policy[TlsPolicy].canonical_visitor_scheme
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
        _refuse_unknown_hosts(unknown)
        if mismatched:
            raise ConflictError(
                "nothing is cached under the scheme requested for: "
                + "; ".join(sorted(mismatched))
                + ". The cache key starts with the scheme, so purging the other "
                "one would report success having removed nothing."
            )
        return tuple(resolved)

    def stats(self, query: UsageQuery) -> PageResult[CacheHour]:
        """Stored hourly cache outcomes, ordered by hour, hostname and edge.

        Read from what traffic accounting committed; no edge is contacted, so
        numbers lag while collection is pending.
        """
        page = self.usage.usage(query)
        return PageResult(
            items=[
                CacheHour(
                    edge=row.edge,
                    hostname=row.hostname,
                    hour=row.hour,
                    outcomes={
                        outcome.value: OutcomeCounters(
                            requests=counters.requests,
                            bytes_sent=counters.bytes_sent,
                        )
                        for outcome, counters in sorted(row.cache.items())
                    },
                )
                for row in page.items
            ],
            total=page.total,
        )

    def connections(
        self, operator: str, *, host_limit: str | None = None
    ) -> ConnectionsReport:
        """Read nginx's live connection counters from the edges, unlocked.

        Counters arrive on each ``HostRun.report``, so an edge that ran but reported
        nothing shows as silent rather than vanishing.
        """
        run = self.runner.run_connections(host_limit=host_limit)
        report = ConnectionsReport(
            collected_at=self.clock(),
            host_limit=host_limit,
            edges=tuple(_edge_connections(host) for host in run.hosts),
        )
        self.events.record(
            domain_event(
                operator,
                "cache.connections_collected",
                "deployment",
                None,
                {
                    "host_limit": host_limit,
                    "reporting": [edge.host for edge in report.reporting],
                    "silent": [edge.host for edge in report.silent],
                },
            )
        )
        if not report.edges:
            raise ExecutionError(run.unreported("connection counters"))
        return report


def _edge_connections(host: HostRun) -> EdgeConnections:
    """Read one edge's report defensively; a malformed one means "nothing usable"."""
    if not host.reached:
        return EdgeConnections(host=host.host, error="unreachable")
    if not host.succeeded:
        return EdgeConnections(
            host=host.host, error="the stats role failed on this edge"
        )
    document = host.report
    if document is None:
        return EdgeConnections(host=host.host, error="the edge published no report")
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
    return EdgeConnections(
        host=host.host,
        collected_at=collected_at,
        nginx_reachable=bool(document.get("nginx_reachable")),
        connections=connections,
    )


class _SiteOwners(NamedTuple):
    """Which enabled site answers for a hostname, wildcards as nginx matches them."""

    exact: dict[str, CdnSite]
    wildcards: tuple[tuple[str, CdnSite], ...]

    def owner_of(self, host: str) -> CdnSite | None:
        return self.exact.get(host) or next(
            (site for suffix, site in self.wildcards if host.endswith(f".{suffix}")),
            None,
        )


def _resolve_purge_tags(tags: Sequence[str]) -> tuple[str, ...]:
    """Tags as the edges match them: validated, lowercased, each named once.

    No site owns a tag, so unlike a prefix there is no hostname to check: a tag
    matches on every hostname the edges cache, and one no origin sends removes
    nothing. The edge reports how many files it removed.
    """
    invalid: list[str] = []
    resolved: list[str] = []
    for tag in tags:
        try:
            resolved.append(purge_tag(tag))
        except ValueError as error:
            invalid.append(f"{tag!r} ({error})")
    if invalid:
        raise ConflictError("invalid purge tags: " + "; ".join(invalid))
    unique = tuple(dict.fromkeys(resolved))
    if len(unique) > MAX_PURGE_TAGS:
        raise ConflictError(
            f"at most {MAX_PURGE_TAGS} tags can be purged at once; "
            f"{len(unique)} were given"
        )
    return unique


def _refuse_unknown_hosts(unknown: set[str]) -> None:
    if unknown:
        raise NotFoundError(
            "no enabled site serves: "
            + ", ".join(sorted(unknown))
            + ". A purge for a hostname nothing serves would report success "
            "having removed nothing."
        )
