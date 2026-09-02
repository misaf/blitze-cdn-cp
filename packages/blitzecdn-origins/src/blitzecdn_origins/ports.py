"""What the origin-check capability needs from the outside world.

The consumer owns the port. These describe what *this* package calls, and
nothing here says how any of them is satisfied — in production by adapters the
control plane already has, in a test by an object that records what it was
asked to run.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from blitzecdn.core.runs import AnsibleRun
from blitzecdn.features.sites.domain import CdnSite


class FleetPlaybooks(Protocol):
    """Run one named play across the edges in scope.

    The single primitive an optional capability borrows from the control
    plane's Ansible adapter, published as ``ControlPlane.fleet``. It is
    deliberately generic: core stages the variables, expands the host limit
    against the fleet it records, and applies the timeout, and it learns
    nothing about what any particular play is for.

    Takes no deployment lock. Everything reached this way is an operation
    rather than a convergence, and an origin check that had to wait for a
    deploy would be useless exactly when it matters — before one.
    """

    def run_playbook(
        self,
        *,
        name: str,
        playbook: Path,
        variables: Mapping[str, object],
        host_limit: str | None = None,
    ) -> AnsibleRun: ...


class OriginCheckRunner(Protocol):
    """The one play this capability runs, in the terms it thinks in.

    ``sites`` is passed rather than read from the desired-state file: this
    takes no deployment lock, so that file may belong to a deploy in flight,
    and the check is a question about what is configured *now* rather than
    about what is being converged.

    The answer comes back on ``HostRun.report``, published by the role as the
    ``blitzecdn_report`` fact. Nothing reads Ansible's textual output.
    """

    def run_origin_check(
        self,
        *,
        sites: Sequence[Mapping[str, object]],
        host_limit: str | None = None,
    ) -> AnsibleRun: ...


class SiteReader(Protocol):
    """The read side of the site model, as the control plane publishes it."""

    def list_sites(self) -> list[CdnSite]: ...


class OriginProbe(Protocol):
    """How to describe a site's origin to whoever is going to connect to it.

    Only ``to_probe`` is borrowed. The control plane's ``OriginProbe`` also
    connects for itself, which is what certificate preflight needs and this
    capability never wants: the whole point of running the play is that the
    edges answer, not the controller.
    """

    def to_probe(self, site: CdnSite) -> dict[str, object]: ...


__all__ = ["FleetPlaybooks", "OriginCheckRunner", "OriginProbe", "SiteReader"]
