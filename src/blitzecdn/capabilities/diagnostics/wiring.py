"""The two health checks: the database must be readable and the queue reachable."""

from __future__ import annotations

from collections.abc import Callable

from dishka import Provider, Scope, provide

from blitzecdn.capabilities.diagnostics.ports import (
    BackupHistory,
    ConvergenceHistory,
    QueueHistory,
)
from blitzecdn.capabilities.diagnostics.service.status import DiagnosticsService
from blitzecdn.capabilities.jobs.ports import WorkerHistory
from blitzecdn.capabilities.tls.ports import CertificateHistory
from blitzecdn.capabilities.workflows.ports import WorkflowHistory
from blitzecdn.core.features import FeatureOperations, HealthCheck
from blitzecdn.core.ports.clock import Clock

__all__ = ["DiagnosticsProvider", "diagnostics_operations"]


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


class DiagnosticsProvider(Provider):
    """The operational status report, read from what other capabilities record."""

    scope = Scope.APP

    @provide
    def service(
        self,
        queue: QueueHistory,
        workflows: WorkflowHistory,
        certificates: CertificateHistory,
        workers: WorkerHistory,
        backups: BackupHistory,
        convergence: ConvergenceHistory,
        clock: Clock,
    ) -> DiagnosticsService:
        return DiagnosticsService(
            queue=queue,
            workflows=workflows,
            certificates=certificates,
            workers=workers,
            backups=backups,
            convergence=convergence,
            clock=clock,
        )
