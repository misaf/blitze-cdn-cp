"""What the cache feature needs from the outside world."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from blitzecdn.core.runs import AnsibleRun
from blitzecdn.features.cache.domain import PurgeEntry

__all__ = ["CacheRunner"]


class CacheRunner(Protocol):
    """Runs the purge and stats plays across the edges in scope.

    Deliberately not the deployment runner. Neither play takes the deployment
    lock and neither writes desired state — a purge has to be able to run
    *while* a deploy is midway through the fleet, which is exactly when a bad
    object being served matters most. Declaring them here rather than reaching
    for ``deployments.ports`` is what keeps this feature independent of the
    deployment package it has no other business with.

    Both answer with an :class:`~blitzecdn.core.runs.AnsibleRun`: stats come
    back on ``HostRun.report``, published by the role as the
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
