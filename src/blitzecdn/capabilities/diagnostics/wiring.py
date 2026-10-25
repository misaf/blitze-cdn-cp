"""The two health checks that decide whether this node can do work at all.

It must be able to read its own database, and it must be able to reach the
queue, because a controller that can accept a deployment and never run it is
worse than one that refuses it. A capability with its own liveness question
contributes its own check rather than editing `/health`.
"""

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
