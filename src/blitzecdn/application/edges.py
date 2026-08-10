"""Operations against edges that are not convergence.

Purging, collecting statistics, probing origins and decommissioning a host all
read or act on the fleet without changing desired state — no deployment record
is written and, deliberately, the deployment lock is not taken.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from blitzecdn.config import Settings
from blitzecdn.domain.models import (
    CacheStatsReport,
    EdgeStats,
    HostDrift,
    OriginCheck,
    PurgeEntry,
    PurgeResult,
    SiteCacheStats,
)
from blitzecdn.domain.recap import parse_play_recap
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
            self._reject_unserved_hosts(entries)
        result = self.runner.run_cache_purge(
            entries=[entry.to_ansible() for entry in entries],
            purge_all=purge_all,
            host_limit=host_limit,
        )
        report = PurgeResult(
            purged_at=datetime.now(UTC),
            entries=tuple(entries),
            purge_all=purge_all,
            host_limit=host_limit,
            hosts=parse_play_recap(result.stdout),
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
            raise ExecutionError(
                "no edge reported a purge result. "
                + (result.stderr.strip() or "Check the inventory and the run above.")
            )
        return report

    def _reject_unserved_hosts(self, entries: Sequence[PurgeEntry]) -> None:
        """Refuse a hostname no enabled site answers to.

        Wildcard server names match their subdomains, the same way nginx
        matches them, so purging ``a.cdn.example.com`` under a ``*.example.com``
        site is allowed.
        """
        served: set[str] = set()
        wildcards: set[str] = set()
        for site in self.sites.list_sites():
            if not site.enabled:
                continue
            for name in site.server_names:
                if name.startswith("*."):
                    wildcards.add(name[2:])
                else:
                    served.add(name)
        unknown = sorted(
            {
                entry.host
                for entry in entries
                if entry.host not in served
                and not any(entry.host.endswith(f".{suffix}") for suffix in wildcards)
            }
        )
        if unknown:
            raise NotFoundError(
                "no enabled site serves: "
                + ", ".join(unknown)
                + ". A purge for a hostname nothing serves would report success "
                "having removed nothing."
            )

    def cache_stats(
        self, operator: str, *, host_limit: str | None = None
    ) -> CacheStatsReport:
        """Collect cache effectiveness from the edges.

        Read-only and unlocked. The edges write one JSON document each into a
        directory under the state dir, which is emptied first so a silent edge
        cannot be answered with its own stale report from a previous run.
        """
        output_dir = self.settings.state_dir / "stats"
        _reset_directory(output_dir)
        result = self.runner.run_stats(output_dir=output_dir, host_limit=host_limit)
        edges = _read_edge_reports(output_dir, parse_play_recap(result.stdout))
        report = CacheStatsReport(
            collected_at=datetime.now(UTC), host_limit=host_limit, edges=edges
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
            raise ExecutionError(
                "no edge reported statistics. "
                + (result.stderr.strip() or "Check the inventory and the run above.")
            )
        return report

    # -- Decommissioning -----------------------------------------------

    def decommission_edge(
        self, name: str, operator: str, *, force: bool = False
    ) -> tuple[HostDrift, ...]:
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

        hosts: tuple[HostDrift, ...] = ()
        failure: str | None = None
        try:
            result = self.runner.run_decommission(host_limit=name)
        except ExecutionError as error:
            failure = str(error)
        else:
            hosts = parse_play_recap(result.stdout)
            # Deliberately not HostDrift.in_sync: a teardown that removed
            # anything reports changed>0, which is what success looks like
            # here. Only failed and unreachable mean files were left behind.
            if result.return_code != 0 or any(
                host.failed or host.unreachable for host in hosts
            ):
                failure = (
                    result.stderr.strip()
                    or "the teardown play did not report a clean host"
                )
            elif not hosts:
                failure = "no edge reported a teardown result"

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


def _reset_directory(path: Path) -> None:
    """Empty a directory, creating it if absent.

    Emptied rather than merged into: an edge that fails to report this run must
    appear as silent, not be answered with the document it left behind last
    time. A stale number read as current is the failure mode that matters here.
    """
    if path.exists():
        for child in path.iterdir():
            if child.is_file():
                child.unlink()
    else:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)


def _read_edge_reports(
    output_dir: Path, recap: Sequence[HostDrift]
) -> tuple[EdgeStats, ...]:
    """Turn the per-edge JSON documents into domain objects.

    Driven by the play recap rather than by whatever files happen to be on
    disk, so an edge that ran but produced nothing is reported as silent
    instead of vanishing from the fleet — the recap is the roster.
    """
    edges: list[EdgeStats] = []
    for host in recap:
        path = output_dir / f"{host.host}.json"
        if host.unreachable:
            edges.append(EdgeStats(host=host.host, error="unreachable"))
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            edges.append(EdgeStats(host=host.host, error=f"no usable report: {exc}"))
            continue
        edges.append(_edge_stats(host.host, document))
    return tuple(edges)


def _edge_stats(host: str, document: object) -> EdgeStats:
    """Read one edge's document defensively.

    The shape is ours, but it crossed a machine boundary and a partially
    written or truncated file must degrade to "this edge said nothing usable"
    rather than raise out of a fleet-wide report.
    """
    if not isinstance(document, dict):
        return EdgeStats(host=host, error="report was not an object")
    outcomes: dict[str, dict[str, int]] = {}
    for row in document.get("cache") or []:
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
    connections = {
        str(key): int(value)
        for key, value in (document.get("connections") or {}).items()
        if isinstance(value, (int, str)) and str(value).isdigit()
    }
    collected = document.get("collected_at")
    try:
        collected_at = datetime.fromisoformat(str(collected)) if collected else None
    except ValueError:
        collected_at = None
    return EdgeStats(
        host=host,
        collected_at=collected_at,
        nginx_reachable=bool(document.get("nginx_reachable")),
        connections=connections,
        sites=tuple(
            SiteCacheStats(site=name, outcomes=counts)
            for name, counts in sorted(outcomes.items())
        ),
    )
