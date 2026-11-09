"""Purges and connection readings as queued operations: their payloads and runs.

The HTTP API queues these for the worker; the CLI calls the service directly.
A payload names the operator and the request; a run returns the same report
the synchronous call does, as JSON.
"""

from __future__ import annotations

from typing import Any, ClassVar

from blitzecdn.capabilities.cache.domain import PurgeEntry, PurgePrefix
from blitzecdn.capabilities.cache.service.cache import CacheService
from blitzecdn.capabilities.jobs import JobPayload, QueuedOperation

__all__ = ["CacheConnectionsJob", "CachePurgeJob", "queued_operations"]


class CachePurgeJob(JobPayload):
    """Purge what an operator asked for, already checked against the sites."""

    kind: ClassVar[str] = "cache.purge"

    operator: str
    entries: tuple[PurgeEntry, ...] = ()
    prefixes: tuple[PurgePrefix, ...] = ()
    tags: tuple[str, ...] = ()
    purge_all: bool = False
    host_limit: str | None = None


class CacheConnectionsJob(JobPayload):
    """Read nginx's live connection counters from the edges."""

    kind: ClassVar[str] = "cache.connections"

    operator: str
    host_limit: str | None = None


def queued_operations(cache: CacheService) -> tuple[QueuedOperation[Any], ...]:
    """Each kind's run, from its payload to its JSON report."""

    def purge(job: CachePurgeJob) -> dict[str, Any]:
        result = cache.purge_cache(
            job.operator,
            entries=job.entries,
            prefixes=job.prefixes,
            tags=job.tags,
            purge_all=job.purge_all,
            host_limit=job.host_limit,
        )
        return result.model_dump(mode="json")

    def connections(job: CacheConnectionsJob) -> dict[str, Any]:
        report = cache.connections(job.operator, host_limit=job.host_limit)
        return report.model_dump(mode="json")

    return (
        QueuedOperation(CachePurgeJob, purge),
        QueuedOperation(CacheConnectionsJob, connections),
    )
