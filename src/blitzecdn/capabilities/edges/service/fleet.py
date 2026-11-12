"""The fleet: who is in it, and what can be done to it short of converging.

Writing an edge publishes it to Ansible, and every change is audited.
Decommissioning writes no deployment and takes no deployment lock.
"""

from __future__ import annotations

from collections.abc import Callable

from blitzecdn.capabilities.edges.domain import (
    LOOPBACK_HOSTS,
    Edge,
    EdgePatch,
    HostKey,
)
from blitzecdn.capabilities.edges.ports import EdgeRunner, EdgeStore, HostKeyStore
from blitzecdn.core.domain.events import domain_event
from blitzecdn.core.domain.preconditions import IfMatch, require_current
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
        host_keys: HostKeyStore,
        uow: UnitOfWork,
    ) -> None:
        self.events = events
        self.runner = runner
        self.edges = edges
        self.host_keys = host_keys
        self.uow = uow

    # -- The roster ----------------------------------------------------

    def list_edges(self) -> list[Edge]:
        """Every edge, which is exactly what Ansible will be given."""
        return self.edges.list_edges()

    def get_edge(self, name: str) -> Edge:
        """One edge; raises ``NotFoundError`` if there is none by that name."""
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

    def update_edge(
        self,
        name: str,
        patch: EdgePatch,
        operator: str,
        *,
        if_match: IfMatch | None = None,
    ) -> Edge:
        """Change an edge's connection or public details; renaming is not possible.

        Certificates, ``--limit`` and audit events refer to the name. Remove and re-add.
        """
        with self.uow.transaction():
            # Read inside the boundary, so ``if_match`` is checked against the
            # version the swap below is made against.
            current = self.edges.get_edge(name)
            require_current(if_match, current, f"edge {name!r}")
            updated = Edge.model_validate(
                {**current.model_dump(), **patch.model_dump(exclude_unset=True)}
            )
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

    def admit_removal[T](
        self, name: str, enqueue: Callable[[], T], *, if_match: IfMatch | None = None
    ) -> T:
        """Queue an edge's removal only if it exists, at the version ``if_match`` names.

        The read, the check and ``enqueue`` share one transaction (nested
        transactions join the outer one), so an edit committed in between cannot
        slip past a stale ``If-Match``.
        """
        with self.uow.transaction():
            require_current(if_match, self.edges.get_edge(name), f"edge {name!r}")
            return enqueue()

    def remove_edge(self, name: str, operator: str) -> None:
        """Stop managing an edge without touching the host, whose keys stay in place."""
        with self.uow.transaction():
            self.edges.delete_edge(name)
            self.events.record(domain_event(operator, "edge.removed", "edge", name))

    # -- SSH host keys -------------------------------------------------

    def manages_host_keys(self, edge: Edge) -> bool:
        """Whether enrolling this edge's key is the controller's job.

        Not in a checkout, where the operator's own SSH client and known_hosts
        decide, and not for the loopback pair the control-plane role records.
        """
        return self.host_keys.managed and edge.host not in LOOPBACK_HOSTS

    def scan_host_keys(self, edge: Edge) -> tuple[HostKey, ...]:
        """What the host at ``edge``'s address offers; nothing is trusted yet."""
        return self.host_keys.scan(edge.host, edge.port)

    def trusted_host_keys(self, edge: Edge) -> tuple[HostKey, ...]:
        """The keys the controller trusts for ``edge``'s address; empty if none."""
        return self.host_keys.trusted(edge.host, edge.port)

    def trust_host_keys(
        self, edge: Edge, keys: tuple[HostKey, ...], operator: str
    ) -> None:
        """Trust ``keys`` for ``edge``'s address, replacing what was trusted before.

        Audited with the fingerprints, so a rotation can be traced to who
        accepted it.
        """
        if not keys:
            raise ConfigurationError("refusing to trust an empty set of host keys")
        with self.uow.transaction():
            self.events.record(
                domain_event(
                    operator,
                    "edge.host_key_trusted",
                    "edge",
                    edge.name,
                    {
                        "host": edge.host,
                        "port": edge.port,
                        "fingerprints": [
                            f"{key.key_type} {key.fingerprint}" for key in keys
                        ],
                    },
                )
            )
            self.host_keys.trust(edge.host, edge.port, keys)

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
