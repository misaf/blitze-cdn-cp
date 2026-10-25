"""Origin-check decisions over consumer-owned runner and reporting ports."""

from __future__ import annotations

from blitzecdn.capabilities.origins.domain import OriginReport, edge_origins
from blitzecdn.capabilities.origins.ports import (
    OriginCheckRunner,
    OriginProbe,
    SiteReader,
)
from blitzecdn.core.domain.events import domain_event
from blitzecdn.core.exceptions import ExecutionError
from blitzecdn.core.ports.clock import Clock, system_clock
from blitzecdn.core.ports.operations import EventRecorder


class OriginCheckService:
    """The fleet's answer about every origin it was asked to reach."""

    def __init__(
        self,
        *,
        sites: SiteReader,
        events: EventRecorder,
        runner: OriginCheckRunner,
        origin_probe: OriginProbe,
        clock: Clock = system_clock,
    ) -> None:
        self.sites = sites
        self.events = events
        self.runner = runner
        self.origin_probe = origin_probe
        self.clock = clock

    def check_origins(
        self, operator: str, *, host_limit: str | None = None
    ) -> OriginReport:
        """Ask every edge to connect to the origins it proxies to.

        From the fleet, per edge and per site; see `origins.domain.report`. Not part of
        ``validate``, which must stay fast and deterministic: run it before a deploy.
        Disabled sites are skipped.
        """
        sites = [site for site in self.sites.list_sites() if site.policy.enabled]
        run = self.runner.run_origin_check(
            sites=[self.origin_probe.to_probe(site) for site in sites],
            host_limit=host_limit,
        )
        report = OriginReport(
            checked_at=self.clock(),
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
