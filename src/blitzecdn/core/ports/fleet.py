"""The fleet, as core needs to see it: host names and their inventory group.

A narrow port so `core.ansible` does not import the `edges` capability;
`blitzecdn.capabilities.edges.adapters.roster.EdgeRoster` implements it.
"""

from __future__ import annotations

from typing import Protocol

__all__ = ["FleetRoster"]


class FleetRoster(Protocol):
    """The hosts a run may target, and the group they form."""

    @property
    def group(self) -> str:
        """The Ansible group every managed host belongs to."""
        ...

    def host_names(self) -> tuple[str, ...]:
        """Every managed host, in a stable order."""
        ...
