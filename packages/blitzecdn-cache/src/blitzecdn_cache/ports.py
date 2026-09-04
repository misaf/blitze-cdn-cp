"""What the cache capability needs from the outside world.

The consumer owns the port. These two describe what *this* package calls, and
nothing here says how it is satisfied — in production by one Ansible adapter
the control plane already has, in a test by an object that records what it was
asked to run.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from blitzecdn.core.domain.runs import AnsibleRun
from blitzecdn_cache.domain import PurgeEntry

__all__ = ["CacheRunner", "FleetPlaybooks"]


class FleetPlaybooks(Protocol):
    """Run one named play across the edges in scope.

    The single primitive an optional capability borrows from the control
    plane's Ansible adapter, published as ``ControlPlane.fleet``. It is
    deliberately generic: core stages the variables, expands the host limit
    against the fleet it records, and applies the timeout, and it learns
    nothing about what any particular play is for. A package that this
    repository has never heard of runs its own play through the same method.

    Takes no deployment lock. Everything reached this way is an operation
    rather than a convergence, and a purge that had to wait for a deploy would
    be useless exactly when it matters.
    """

    def run_playbook(
        self,
        *,
        name: str,
        playbook: Path,
        variables: Mapping[str, object],
        host_limit: str | None = None,
    ) -> AnsibleRun: ...


class CacheRunner(Protocol):
    """The two plays this capability runs, in the terms it thinks in.

    Both answer with an :class:`~blitzecdn.core.domain.runs.AnsibleRun`: statistics
    come back on ``HostRun.report``, published by the role as the
    ``blitzecdn_report`` fact, and nothing here reads Ansible's output.
    """

    def run_cache_purge(
        self,
        *,
        entries: Sequence[PurgeEntry],
        purge_all: bool,
        host_limit: str | None = None,
    ) -> AnsibleRun: ...

    def run_stats(self, *, host_limit: str | None = None) -> AnsibleRun: ...
