"""The teardown play this capability runs, over core's generic play primitive."""

from __future__ import annotations

from pathlib import Path

from blitzecdn.capabilities.edges.ports import FleetPlaybooks
from blitzecdn.core.domain.runs import AnsibleRun

__all__ = ["DecommissionPlaybook"]


class DecommissionPlaybook:
    """``EdgeRunner``: strip BlitzeCDN configuration and TLS material from an edge."""

    def __init__(self, fleet: FleetPlaybooks, playbook: Path) -> None:
        self._fleet = fleet
        self._playbook = playbook

    def run_decommission(self, *, host_limit: str) -> AnsibleRun:
        """``host_limit`` is required: no limit means every edge, which for a
        teardown would empty the fleet."""
        return self._fleet.run_playbook(
            name="decommission",
            playbook=self._playbook,
            variables={},
            host_limit=host_limit,
        )
