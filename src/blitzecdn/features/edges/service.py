"""The fleet: who is in it, and what can be done to it short of converging.

Registering, updating and removing an edge changes *which hosts exist* — and
because the Ansible inventory is read from the same rows, writing one here is
what publishes it to Ansible. Probing origins and decommissioning read or act
on the fleet without changing desired state; those write no deployment record
and, deliberately, do not take the deployment lock.

Registration goes through a service rather than straight to the store so that
both entry points reach it the same way and every change is audited: "who added
this edge, and when" is a question the audit trail can answer.

What is *stored* on the edges is a different question and lives in
:mod:`blitzecdn.features.cache.service`. Purging and cache statistics were here only
because they also reach the fleet, which describes the transport rather than
the work.
"""

from __future__ import annotations

from datetime import UTC, datetime

from blitzecdn.core.events import domain_event
from blitzecdn.core.exceptions import ConfigurationError, ExecutionError
from blitzecdn.core.operation_ports import EventRecorder
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.runs import HostRun
from blitzecdn.features.deployments.ports import DeploymentRunner
from blitzecdn.features.dns.ports import SiteStore
from blitzecdn.features.edges.domain import Edge, EdgePatch
from blitzecdn.features.edges.origins import OriginReport
from blitzecdn.features.edges.ports import EdgeStore, OriginProbe
from blitzecdn.features.edges.reporting import edge_origins


class EdgeOperationsService:
    """The fleet roster, and the operations that read or clean it."""

    def __init__(
        self,
        *,
        sites: SiteStore,
        events: EventRecorder,
        runner: DeploymentRunner,
        origin_probe: OriginProbe,
        edges: EdgeStore,
        uow: UnitOfWork,
    ) -> None:
        self.sites = sites
        self.events = events
        self.runner = runner
        self.origin_probe = origin_probe
        self.edges = edges
        self.uow = uow

    # -- The roster ----------------------------------------------------

    def list_edges(self) -> list[Edge]:
        """Every edge, which is exactly what Ansible will be given."""
        return self.edges.list_edges()

    def get_edge(self, name: str) -> Edge:
        return self.edges.get_edge(name)

    def add_edge(self, edge: Edge, operator: str) -> Edge:
        """Register an edge. It joins the fleet on the next run, not this one.

        Nothing is converged here and nothing reaches the host. The edge simply
        exists from now on, which means the next deploy includes it — so a new
        edge goes from registered to serving in one ``blitzecdn deploy``,
        rather than needing an inventory file to be written first and hoping
        the two agree.
        """
        with self.uow.transaction():
            created = self.edges.create_edge(edge)
            self.events.record(
                domain_event(
                    operator,
                    "edge.added",
                    "edge",
                    created.name,
                    {
                        "host": created.host,
                        "user": created.user,
                        "port": created.port,
                        "public_addresses": list(created.effective_public_addresses),
                        "ssh_sources": list(created.ssh_sources),
                    },
                )
            )
        return created

    def update_edge(self, name: str, patch: EdgePatch, operator: str) -> Edge:
        """Change an existing edge's connection or public details.

        Renaming is deliberately not possible. The name is how a certificate
        preflight, a ``--limit`` and every audit event so far refer to this
        host; a rename would silently orphan all of them. Remove and re-add.
        """
        current = self.edges.get_edge(name)
        updated = Edge.model_validate(
            {**current.model_dump(), **patch.model_dump(exclude_unset=True)}
        )
        with self.uow.transaction():
            saved = self.edges.replace_edge(updated, expected=current)
            self.events.record(
                domain_event(
                    operator,
                    "edge.updated",
                    "edge",
                    saved.name,
                    {"fields": sorted(patch.model_fields_set)},
                )
            )
        return saved

    def remove_edge(self, name: str, operator: str) -> None:
        """Stop managing an edge without touching the host.

        The narrow case: a host already wiped by other means, or one that no
        longer exists. Everything BlitzeCDN put on a live host — its
        configuration, and the private keys for every certificate it serves —
        stays exactly where it is, which is why ``decommission_edge`` is what
        the CLI reaches for by default.
        """
        with self.uow.transaction():
            self.edges.delete_edge(name)
            self.events.record(domain_event(operator, "edge.removed", "edge", name))

    # -- Origins -------------------------------------------------------

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
        if name not in {edge.name for edge in self.edges.list_edges()}:
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
                failure = run.unreported("teardown")
            elif not run.succeeded or any(not host.succeeded for host in hosts):
                # run.summary() names the task that failed, on the host it
                # failed on. That is the difference between "teardown failed"
                # and "teardown failed removing /etc/blitzecdn on edge-b".
                failure = run.summary()

        if failure is not None and not force:
            self.events.record(
                domain_event(
                    operator,
                    "edge.decommission_failed",
                    "edge",
                    name,
                    {
                        "forced": force,
                        "hosts": [host.host for host in hosts],
                        "error": failure,
                    },
                )
            )
            raise ExecutionError(
                f"teardown of {name} failed and the inventory entry was kept: "
                f"{failure} Retry once the host is reachable, or pass --force "
                "if the host no longer exists — forcing leaves its "
                "configuration and private keys in place."
            )
        with self.uow.transaction():
            self.edges.delete_edge(name)
            self.events.record(
                domain_event(
                    operator,
                    (
                        "edge.decommissioned"
                        if failure is None
                        else "edge.decommission_failed"
                    ),
                    "edge",
                    name,
                    {
                        "forced": force,
                        "hosts": [host.host for host in hosts],
                        **({} if failure is None else {"error": failure}),
                    },
                )
            )
        return hosts
