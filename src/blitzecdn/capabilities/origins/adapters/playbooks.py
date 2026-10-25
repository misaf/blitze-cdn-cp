"""The one play this capability runs, and the document it is given.

Core keeps only the generic primitive; this adapter is also handed to the
Automatic SSL/TLS scan, so there is one way to ask the fleet about origins.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from blitzecdn.capabilities.origins import ansible
from blitzecdn.capabilities.origins.ports import FleetPlaybooks
from blitzecdn.core.domain.runs import AnsibleRun

__all__ = ["OriginCheckPlaybook"]


class OriginCheckPlaybook:
    """``OriginCheckRunner``, over the control plane's generic play primitive."""

    def __init__(self, fleet: FleetPlaybooks) -> None:
        self._fleet = fleet

    def run_origin_check(
        self,
        *,
        sites: Sequence[Mapping[str, object]],
        host_limit: str | None = None,
    ) -> AnsibleRun:
        """Ask each edge in scope to connect to the origins it proxies to.

        Sites travel as run variables: no lock is taken, so the desired-state file may
        belong to a deploy in flight.
        """
        return self._fleet.run_playbook(
            name="origin-check",
            playbook=ansible.ORIGIN_CHECK_PLAYBOOK,
            variables={"blitzecdn_origins_sites": list(sites)},
            host_limit=host_limit,
        )
