from __future__ import annotations

from typing import Protocol

from blitzecdn.features.dns.site_domain import CdnSite
from blitzecdn.features.edges.domain import Edge
from blitzecdn.features.edges.origins import OriginCheck


class EdgeStore(Protocol):
    """The fleet. The Ansible inventory is derived from this, not the reverse.

    The ``blitzecdn`` inventory plugin reads these same rows at the start of
    every run, so writing an edge here *is* publishing it to Ansible. There is
    no second artefact that could disagree with the database about which hosts
    exist.
    """

    def list_edges(self) -> list[Edge]: ...

    def get_edge(self, name: str) -> Edge: ...

    def create_edge(self, edge: Edge) -> Edge: ...

    def replace_edge(self, edge: Edge, *, expected: Edge | None = None) -> Edge: ...

    def delete_edge(self, name: str) -> None: ...


# ----------------------------------------------------------------------
# Certificates
# ----------------------------------------------------------------------


class OriginProbe(Protocol):
    """A site's origin: how to reach it, and what the controller sees of it.

    ``to_probe`` renders an origin for whoever is going to connect to it. The
    operator-facing check is answered by the *edges* — ``check_origins`` runs a
    playbook, because the controller's routes, resolver and egress rules are not
    the ones that carry traffic, and an origin allow-listing the edges refuses
    the controller while working perfectly.

    ``check`` is the controller connecting for itself, and survives for exactly
    one caller: the advisory origin check inside certificate preflight, which
    has to answer in milliseconds during issuance and cannot run a playbook. It
    is advisory there precisely because of the vantage point.
    """

    def to_probe(self, site: CdnSite) -> dict[str, object]: ...

    def check(self, site: CdnSite) -> OriginCheck: ...


__all__ = ["EdgeStore", "OriginProbe"]
