from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from blitzecdn.capabilities.edges.domain import Edge, HostKey
from blitzecdn.capabilities.origins.contract import OriginCheck
from blitzecdn.core.domain.runs import AnsibleRun
from blitzecdn.core.domain.site import CdnSite


class FleetPlaybooks(Protocol):
    """Run one named play across the edges in scope, without the deployment lock."""

    def run_playbook(
        self,
        *,
        name: str,
        playbook: Path,
        variables: Mapping[str, object],
        host_limit: str | None = None,
    ) -> AnsibleRun:
        """Run ``playbook`` across the edges ``host_limit`` selects, or all of them.

        ``variables`` are staged for this run alone and ``name`` labels its log. No
        deployment lock is taken: these plays write no desired state.
        """
        ...


class EdgeRunner(Protocol):
    """What edge operations run against the fleet; ``host_limit`` is always required."""

    def run_decommission(self, *, host_limit: str) -> AnsibleRun:
        """Remove BlitzeCDN from the edges ``host_limit`` selects.

        Required: no limit means every edge, which for a teardown empties the fleet.
        """
        ...


class EdgeStore(Protocol):
    """The fleet; writing an edge here is publishing it to the Ansible inventory."""

    def list_edges(self) -> list[Edge]:
        """Every edge, ordered by name."""
        ...

    def get_edge(self, name: str) -> Edge:
        """One edge; raises ``NotFoundError`` if there is none by that name."""
        ...

    def create_edge(self, edge: Edge) -> Edge:
        """Add an edge; raises ``ConflictError`` if the name is taken."""
        ...

    def replace_edge(self, edge: Edge, *, expected: Edge | None = None) -> Edge:
        """Write an edge in place; its name is its identity and never moves.

        Pass ``expected``, the edge the change was merged onto, to make it a
        compare-and-swap that raises ``ConflictError`` if the row moved since.
        Raises ``NotFoundError`` if the edge is absent.
        """
        ...

    def delete_edge(self, name: str) -> None:
        """Remove an edge; raises ``NotFoundError`` if there is none by that name."""
        ...


class HostKeyStore(Protocol):
    """The SSH host keys the controller trusts for its edges.

    ``managed`` is false where the controller has no SSH configuration of its
    own — a checkout, where the operator's SSH client and ``known_hosts`` decide.
    """

    @property
    def managed(self) -> bool: ...

    def scan(self, host: str, port: int) -> tuple[HostKey, ...]:
        """Every key the host offers. Unverified: what it says, not what it is."""
        ...

    def trusted(self, host: str, port: int) -> tuple[HostKey, ...]:
        """The keys trusted for ``host`` on ``port``; empty when none are."""
        ...

    def trust(self, host: str, port: int, keys: tuple[HostKey, ...]) -> None:
        """Trust exactly ``keys`` for ``host`` on ``port``, replacing any before."""
        ...


# ----------------------------------------------------------------------
# Certificates
# ----------------------------------------------------------------------


class OriginProbe(Protocol):
    """A site's origin: how to reach it, and what the controller sees of it.

    ``to_probe`` renders it for the edges; ``check`` serves only certificate
    preflight's advisory check.
    """

    def to_probe(self, site: CdnSite) -> dict[str, object]:
        """Render ``site``'s canonical origin as the edge-side check reads it."""
        ...

    def check(self, site: CdnSite) -> OriginCheck:
        """Connect to ``site``'s origin from the controller, as an edge would.

        Advisory only: certificate preflight reports it; nothing branches on it.
        """
        ...


__all__ = [
    "EdgeRunner",
    "EdgeStore",
    "FleetPlaybooks",
    "HostKeyStore",
    "OriginProbe",
]
