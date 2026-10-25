"""The two health checks: the database must be readable and the queue reachable."""

from __future__ import annotations

from collections.abc import Callable

from blitzecdn.core.features import FeatureOperations, HealthCheck


def diagnostics_operations(
    *, read_database: Callable[[], object], queue_ready: Callable[[], bool]
) -> FeatureOperations:
    """``read_database`` raises when the database cannot be read."""

    def database() -> None:
        read_database()

    def queue() -> None:
        if not queue_ready():
            raise ConnectionError("the durable job table could not be read")

    return FeatureOperations(
        feature="diagnostics",
        health_checks=(
            HealthCheck(feature="diagnostics", name="database", check=database),
            HealthCheck(feature="diagnostics", name="queue", check=queue),
        ),
    )
