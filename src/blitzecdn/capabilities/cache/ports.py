"""What the cache capability needs from the outside world."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from blitzecdn.capabilities.cache.domain import PurgeEntry
from blitzecdn.core.domain.runs import AnsibleRun

__all__ = ["CacheRunner", "FleetPlaybooks"]


class FleetPlaybooks(Protocol):
    """Run one named play across the edges in scope, without the deployment lock.

    Core's generic primitive: it stages variables, expands the limit and applies
    the timeout, knowing nothing of the play.
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
    """The two plays this capability runs; statistics arrive on ``HostRun.report``."""

    def run_cache_purge(
        self,
        *,
        entries: Sequence[PurgeEntry],
        purge_all: bool,
        host_limit: str | None = None,
    ) -> AnsibleRun: ...

    def run_stats(self, *, host_limit: str | None = None) -> AnsibleRun: ...
