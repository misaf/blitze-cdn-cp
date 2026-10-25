"""Purges and statistics as queued operations: their kinds, payloads and runs.

The HTTP API queues these for the worker; the CLI calls the service directly.
A payload is JSON naming the operator and the request; a run returns the same
report the synchronous call does, as JSON.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from blitzecdn.capabilities.cache.domain import PurgeEntry
from blitzecdn.capabilities.cache.service.cache import CacheService

__all__ = [
    "OPERATION_KINDS",
    "PURGE_OPERATION",
    "STATS_OPERATION",
    "operation_handlers",
    "purge_payload",
    "stats_payload",
]

PURGE_OPERATION = "cache.purge"
STATS_OPERATION = "cache.stats"
OPERATION_KINDS = (PURGE_OPERATION, STATS_OPERATION)


def purge_payload(
    operator: str,
    *,
    entries: Sequence[PurgeEntry],
    purge_all: bool,
    host_limit: str | None,
) -> dict[str, Any]:
    return {
        "operator": operator,
        "entries": [entry.model_dump(mode="json") for entry in entries],
        "purge_all": purge_all,
        "host_limit": host_limit,
    }


def stats_payload(operator: str, *, host_limit: str | None) -> dict[str, Any]:
    return {"operator": operator, "host_limit": host_limit}


def operation_handlers(
    cache: CacheService,
) -> dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]]:
    """Each kind's run, from its payload to its JSON report."""

    def purge(payload: Mapping[str, Any]) -> dict[str, Any]:
        result = cache.purge_cache(
            str(payload["operator"]),
            entries=[PurgeEntry.model_validate(entry) for entry in payload["entries"]],
            purge_all=bool(payload["purge_all"]),
            host_limit=payload.get("host_limit"),
        )
        return result.model_dump(mode="json")

    def stats(payload: Mapping[str, Any]) -> dict[str, Any]:
        report = cache.cache_stats(
            str(payload["operator"]), host_limit=payload.get("host_limit")
        )
        return report.model_dump(mode="json")

    return {PURGE_OPERATION: purge, STATS_OPERATION: stats}
