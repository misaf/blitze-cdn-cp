"""Operations against edges that are not convergence.

Purging, collecting statistics, probing origins and decommissioning a host all
read or act on the fleet without changing desired state — no deployment record
is written and, deliberately, the deployment lock is not taken.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from blitzecdn.config import Settings
from blitzecdn.domain.cache import (
    CacheStatsReport,
    EdgeStats,
    PurgeEntry,
    PurgeResult,
    SiteCacheStats,
)
from blitzecdn.domain.origins import OriginCheck
from blitzecdn.domain.runs import AnsibleRun, HostRun
from blitzecdn.domain.sites import CdnSite, CertificateMode, HttpScheme
from blitzecdn.exceptions import (
    ConfigurationError,
    ConflictError,
    ExecutionError,
    NotFoundError,
)
from blitzecdn.ports import (
    AuditLog,
    DeploymentRunner,
    EdgeInventory,
    OriginProbe,
    SiteStore,
)


class EdgeOperationsService:
    """Fleet operations that read or clean, rather than converge."""

    def __init__(
        self,
        settings: Settings,
        sites: SiteStore,
        audit_log: AuditLog,
        runner: DeploymentRunner,
        origin_probe: OriginProbe,
        inventory: EdgeInventory,
    ) -> None:
        self.settings = settings
        self.sites = sites
        self.audit_log = audit_log
        self.runner = runner
        self.origin_probe = origin_probe
        self.inventory = inventory

    # -- Origins -------------------------------------------------------

    def check_origins(self) -> list[OriginCheck]:
        """Connect to every enabled site's origin the way the edge will.

        Deliberately not folded into ``validate()``, which ``deploy`` runs.
        Validation is about desired state being coherent and has to stay fast
        and deterministic; an origin being briefly unreachable is neither a
        reason to refuse a deploy of unrelated sites nor something a deploy
        should wait on. Run this before a deploy, not inside one.

        Disabled sites are skipped: the edge will not proxy to them, so their
        origins being down is not a fact about anything.
        """
        return self.origin_probe.check_all(
            [site for site in self.sites.list_sites() if site.enabled]
        )

    # -- Cache ---------------------------------------------------------

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
        if entries:
            self._reject_unpurgeable(entries)
        run = self.runner.run_cache_purge(
            entries=[entry.to_ansible() for entry in entries],
            purge_all=purge_all,
            host_limit=host_limit,
        )
        report = PurgeResult(
            purged_at=datetime.now(UTC),
            entries=tuple(entries),
            purge_all=purge_all,
            host_limit=host_limit,
            hosts=run.hosts,
        )
        self.audit_log.audit(
            operator,
            "cache.purged",
            "site",
            None,
            {
                "purge_all": purge_all,
                "entries": [entry.to_ansible() for entry in entries],
                "host_limit": host_limit,
                "complete": report.complete,
                "failed": [host.host for host in report.failed],
            },
        )
        if not report.hosts:
            raise ExecutionError(_unreported("purge", run))
        return report

    def _reject_unpurgeable(self, entries: Sequence[PurgeEntry]) -> None:
        """Refuse an entry no enabled site could have cached.

        Two ways that happens, and either would otherwise report a successful
        purge of nothing.

        The hostname may be one no site answers to. Wildcard server names match
        their subdomains, the same way nginx matches them, so purging
        ``a.cdn.example.com`` under a ``*.example.com`` site is allowed.

        Or the scheme may be one the site never serves. The cache key begins
        with ``$scheme``, so the two are different entries: a site with TLS
        answers port 80 with a 308 and caches nothing under ``http``, and a site
        without TLS never sees an ``https`` request at all. Purging the wrong one
        computes a different MD5, deletes a file that was never written, and
        reports every edge as purged.
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
            served = (
                HttpScheme.HTTP
                if owner.certificate_mode is CertificateMode.DISABLED
                else HttpScheme.HTTPS
            )
            if entry.scheme is not served:
                mismatched.add(
                    f"{entry.scheme.value}://{entry.host}{entry.uri} "
                    f"({owner.name} serves {served.value})"
                )
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
        self.audit_log.audit(
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
        if not report.edges:
            raise ExecutionError(_unreported("statistics", run))
        return report

    # -- Decommissioning -----------------------------------------------

    def decommission_edge(
        self, name: str, operator: str, *, force: bool = False
    ) -> tuple[HostRun, ...]:
        """Strip an edge of BlitzeCDN state, then take it out of inventory.

        The order is the whole point. Removing the inventory entry first would
        leave a host that no playbook can address again, still serving the
        virtual hosts it last converged and still holding the private keys for
        them. So the teardown runs while the host is still reachable, and the
        entry is deleted only once it reports clean.

        ``force`` drops the entry after a failed or unreachable teardown. It is
        for a host that no longer exists — a destroyed instance cannot be
        cleaned and would otherwise block its own removal forever. On a host
        that is merely down it is the wrong answer: the keys stay where they
        are and nothing will ever come back for them.
        """
        if name not in {edge["name"] for edge in self.inventory.list_edges()}:
            raise ConfigurationError(f"edge does not exist: {name}")

        hosts: tuple[HostRun, ...] = ()
        failure: str | None = None
        try:
            run = self.runner.run_decommission(host_limit=name)
        except ExecutionError as error:
            failure = str(error)
        else:
            hosts = run.hosts
            # Deliberately not HostRun.in_sync: a teardown that removed
            # anything reports changed>0, which is what success looks like
            # here. Only failed and unreachable mean files were left behind.
            if not hosts:
                failure = _unreported("teardown", run)
            elif not run.succeeded or any(not host.succeeded for host in hosts):
                # run.summary() names the task that failed, on the host it
                # failed on. That is the difference between "teardown failed"
                # and "teardown failed removing /etc/blitzecdn on edge-b".
                failure = run.summary()

        self.audit_log.audit(
            operator,
            "edge.decommissioned" if failure is None else "edge.decommission_failed",
            "edge",
            name,
            {
                "forced": force,
                "hosts": [host.host for host in hosts],
                **({} if failure is None else {"error": failure}),
            },
        )
        if failure is not None and not force:
            raise ExecutionError(
                f"teardown of {name} failed and the inventory entry was kept: "
                f"{failure} Retry once the host is reachable, or pass --force "
                "if the host no longer exists — forcing leaves its "
                "configuration and private keys in place."
            )
        self.inventory.remove_edge(name)
        return hosts


def _unreported(what: str, run: AnsibleRun) -> str:
    """Explain a run that produced no per-host result.

    Every fleet operation needs this message and each used to build its own out
    of whatever was on stderr. There is no stderr to reach for now, and the
    honest answer is the same in all three cases: nothing came back, here is
    what the process said about itself, and here is where the output is.
    """
    detail = run.error or run.summary()
    location = f" The full output is at {run.log_path}." if run.log_path else ""
    return f"no edge reported a {what} result. {detail}{location}"


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
