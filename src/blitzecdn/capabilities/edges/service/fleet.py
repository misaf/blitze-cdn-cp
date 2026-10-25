"""The fleet: who is in it, and what can be done to it short of converging.

Writing an edge publishes it to Ansible, and every change is audited.
Decommissioning writes no deployment and takes no deployment lock.
"""

from __future__ import annotations

from blitzecdn.capabilities.edges.domain import Edge, EdgePatch
from blitzecdn.capabilities.edges.ports import EdgeRunner, EdgeStore
from blitzecdn.core.domain.events import domain_event
from blitzecdn.core.domain.runs import HostRun
from blitzecdn.core.exceptions import ConfigurationError, ExecutionError
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.ports.operations import EventRecorder


class EdgeOperationsService:
    """The fleet roster, and the operations that read or clean it."""

    def __init__(
        self,
        *,
        events: EventRecorder,
        runner: EdgeRunner,
        edges: EdgeStore,
        uow: UnitOfWork,
    ) -> None:
        self.events = events
        self.runner = runner
        self.edges = edges
        self.uow = uow

    # -- The roster ----------------------------------------------------

    def list_edges(self) -> list[Edge]:
        """Every edge, which is exactly what Ansible will be given."""
        return self.edges.list_edges()

    def get_edge(self, name: str) -> Edge:
        return self.edges.get_edge(name)

    def add_edge(self, edge: Edge, operator: str) -> Edge:
        """Register an edge; it joins the fleet on the next deploy."""
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
        """Change an edge's connection or public details; renaming is not possible.

        Certificates, ``--limit`` and audit events refer to the name. Remove and re-add.
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
        """Stop managing an edge without touching the host, whose keys stay in place."""
        with self.uow.transaction():
            self.edges.delete_edge(name)
            self.events.record(domain_event(operator, "edge.removed", "edge", name))

    # -- Decommissioning -----------------------------------------------

    def decommission_edge(
        self, name: str, operator: str, *, force: bool = False
    ) -> tuple[HostRun, ...]:
        """Strip an edge of BlitzeCDN state, then take it out of inventory.

        Teardown first, while the host is still addressable; the entry goes only once
        it reports clean. ``force`` drops it anyway, for a destroyed host — on one that
        is merely down, its keys stay behind for good.
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
            # Not `in_sync`: a teardown that removed things reports changes. Only failed
            # and unreachable mean files were left.
            if not hosts:
                failure = run.unreported("teardown")
            elif not run.succeeded or any(not host.succeeded for host in hosts):
                # run.summary() names the failed task and host.
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
