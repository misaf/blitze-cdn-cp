"""The fleet, as core needs to see it.

Core runs Ansible against the hosts the control plane records, and for that it
needs two facts and no more: what the hosts are called, and which inventory
group they form. It used to take `EdgeStore` — a capability's full read/write
port, five methods of which one was ever called — and with it
`blitzecdn.capabilities.edges.domain.Edge`, so `core.ansible` could not be
imported without importing a capability.

A roster is what an adapter for that store looks like from below.
`blitzecdn.capabilities.edges.adapters.roster.EdgeRoster` is the only
implementation, and the direction of the arrow is the whole point of the
separation: `edges` knows about core, core does not know about `edges`.
"""

from __future__ import annotations

from typing import Protocol

__all__ = ["FleetRoster"]


class FleetRoster(Protocol):
    """The hosts a run may target, and the group they form.

    ``group`` is here rather than as a constant in core because the name is
    the roster's to give: it is what the inventory publishing these hosts
    calls them collectively, and core only has to pass it to ``--limit`` when
    an operator named no host at all.
    """

    @property
    def group(self) -> str:
        """The Ansible group every managed host belongs to."""
        ...

    def host_names(self) -> tuple[str, ...]:
        """Every managed host, in a stable order."""
        ...
