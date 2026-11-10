"""What the cache capability needs from the outside world."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from blitzecdn.capabilities.cache.domain import PurgeEntry, PurgePrefix
from blitzecdn.capabilities.traffic import TrafficUsage, UsageQuery
from blitzecdn.core.domain.pagination import PageResult
from blitzecdn.core.domain.runs import AnsibleRun

__all__ = ["CacheRunner", "CacheUsage", "FleetPlaybooks"]


class CacheUsage(Protocol):
    """Stored hourly counters, cache outcomes included, read without edges.

    Traffic accounting owns the log cursor; counting cache outcomes in the
    same pass means every record is counted exactly once, for both.
    """

    def usage(self, query: UsageQuery) -> PageResult[TrafficUsage]:
        """One page of stored usage, cache outcomes included, matching ``query``."""
        ...


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
    ) -> AnsibleRun:
        """Run ``playbook`` across the edges ``host_limit`` selects, or all of them.

        ``variables`` are staged for this run alone and ``name`` labels its log. No
        deployment lock is taken: these plays write no desired state.
        """
        ...


class CacheRunner(Protocol):
    """The two plays this capability runs; counters arrive on ``HostRun.report``."""

    def run_cache_purge(
        self,
        *,
        entries: Sequence[PurgeEntry],
        prefixes: Sequence[PurgePrefix],
        tags: Sequence[str],
        purge_all: bool,
        host_limit: str | None = None,
    ) -> AnsibleRun:
        """Purge cached responses on the edges ``host_limit`` selects, or all.

        By exact ``entries``, by URL ``prefixes`` and by ``tags``, or everything when
        ``purge_all`` is set. Takes no deployment lock.
        """
        ...

    def run_connections(self, *, host_limit: str | None = None) -> AnsibleRun:
        """Read each selected edge's connection counters. Takes no deployment lock."""
        ...
