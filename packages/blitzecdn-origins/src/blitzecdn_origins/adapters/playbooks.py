"""The one play this capability runs, and the document it is given.

This is the seam the extraction was about. ``run_origin_check`` used to be a
method on core's ``AnsibleRunner`` and the play's location a field on
``Settings``, which meant the shared Ansible adapter — the one every capability
reaches through — carried an operation only this capability performs, pointing
at a file only this wheel ships. Core kept the generic primitive: run this
play, with these variables, against these hosts.

It is also what makes the play reachable from *two* distributions without
either knowing the other's internals. ``blitzecdn-certificates`` builds this
same adapter in its own composition root for the Automatic SSL/TLS scan; there
is one implementation of "ask the fleet about these origins" rather than a copy
per caller.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from blitzecdn.core.domain.runs import AnsibleRun
from blitzecdn_origins import ansible
from blitzecdn_origins.ports import FleetPlaybooks

__all__ = ["OriginCheckPlaybook"]


class OriginCheckPlaybook:
    """``OriginCheckRunner``, over the control plane's generic play primitive.

    The play and the role it names ship inside this wheel and are located
    through :mod:`blitzecdn_origins.ansible`. Core is told the *path* — it
    stages the variables, expands the host limit against the fleet it records,
    and applies the timeout — and never what the play is for.
    """

    def __init__(self, fleet: FleetPlaybooks) -> None:
        self._fleet = fleet

    def run_origin_check(
        self,
        *,
        sites: Sequence[Mapping[str, object]],
        host_limit: str | None = None,
    ) -> AnsibleRun:
        """Ask each edge in scope to connect to the origins it proxies to.

        The sites travel as run variables rather than being read from the
        desired-state file: this takes no deployment lock, so that shared file
        may belong to a deploy in flight, and the check is a question about
        what is configured *now* rather than about what is being converged.
        """
        return self._fleet.run_playbook(
            name="origin-check",
            playbook=ansible.ORIGIN_CHECK_PLAYBOOK,
            variables={"blitzecdn_origins_sites": list(sites)},
            host_limit=host_limit,
        )
