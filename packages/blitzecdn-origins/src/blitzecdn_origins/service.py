"""Ask the fleet whether it can reach the origins it proxies to.

Moved here from ``EdgeOperationsService`` with the play and the role it runs.
The fleet *roster* — which hosts exist, registering one, decommissioning one —
is core's and stayed there: an installation must be able to add and remove
edges with no optional distribution attached. Asking those edges a question
about the world is an operation, and operations are wheels.
"""

from __future__ import annotations

from datetime import UTC, datetime

from blitzecdn.core.domain.events import domain_event
from blitzecdn.core.exceptions import ExecutionError
from blitzecdn.core.ports.operations import EventRecorder
from blitzecdn_origins.domain import OriginReport
from blitzecdn_origins.ports import OriginCheckRunner, OriginProbe, SiteReader
from blitzecdn_origins.reporting import edge_origins


class OriginCheckService:
    """The fleet's answer about every origin it was asked to reach."""

    def __init__(
        self,
        *,
        sites: SiteReader,
        events: EventRecorder,
        runner: OriginCheckRunner,
        origin_probe: OriginProbe,
    ) -> None:
        self.sites = sites
        self.events = events
        self.runner = runner
        self.origin_probe = origin_probe

    def check_origins(
        self, operator: str, *, host_limit: str | None = None
    ) -> OriginReport:
        """Ask every edge to connect to the origins it proxies to.

        Answered by the fleet rather than by the controller. The controller's
        routes, resolver and egress rules are not the ones that carry traffic:
        an origin allow-listing the edges' addresses refuses the controller
        while working perfectly, and one reachable only from the controller's
        subnet passes and then 502s on every edge. Both were this check
        reporting confidently on a question nobody asked.

        Per edge *and* per site for the same reason. An origin no edge can
        reach is down; one some edges can reach is a routing or allow-list
        problem, and a single vantage point could never tell those apart.

        Deliberately not folded into ``validate()``, which ``deploy`` runs.
        Validation is about desired state being coherent and has to stay fast
        and deterministic; an origin being briefly unreachable is neither a
        reason to refuse a deploy of unrelated sites nor something a deploy
        should wait on. Run this before a deploy, not inside one.

        Disabled sites are skipped: the edge will not proxy to them, so their
        origins being down is not a fact about anything.
        """
        sites = [site for site in self.sites.list_sites() if site.enabled]
        run = self.runner.run_origin_check(
            sites=[self.origin_probe.to_probe(site) for site in sites],
            host_limit=host_limit,
        )
        report = OriginReport(
            checked_at=datetime.now(UTC),
            host_limit=host_limit,
            edges=tuple(edge_origins(host) for host in run.hosts),
        )
        self.events.record(
            domain_event(
                operator,
                "origins.checked",
                "site",
                None,
                {
                    "host_limit": host_limit,
                    "sites": len(sites),
                    "silent": [edge.host for edge in report.silent],
                    "failing": {
                        site: list(hosts)
                        for site, hosts in report.failing_sites.items()
                    },
                },
            )
        )
        if not report.edges:
            raise ExecutionError(run.unreported("origin check"))
        return report


__all__ = ["OriginCheckService"]
