"""Combine observations without running remote probes or changing state."""

from blitzecdn.capabilities.diagnostics.domain.status import (
    WORKER_STALE_SECONDS,
    CertificateStatistics,
    OperationalStatus,
    WorkerStatus,
)
from blitzecdn.capabilities.diagnostics.ports import QueueHistory
from blitzecdn.capabilities.jobs.ports import WorkerHistory
from blitzecdn.capabilities.tls.ports import CertificateHistory
from blitzecdn.capabilities.workflows.ports import WorkflowHistory
from blitzecdn.core.ports.clock import Clock, system_clock


class DiagnosticsService:
    def __init__(
        self,
        *,
        queue: QueueHistory,
        workflows: WorkflowHistory,
        certificates: CertificateHistory,
        workers: WorkerHistory,
        clock: Clock = system_clock,
    ) -> None:
        self.queue = queue
        self.workflows = workflows
        self.certificates = certificates
        self.workers = workers
        self.clock = clock

    def status(self) -> OperationalStatus:
        now = self.clock()
        observations = self.workers.read()
        workers = []
        for worker in observations.workers:
            age = worker.age_seconds(now)
            workers.append(
                WorkerStatus(
                    **worker.model_dump(),
                    state="stale"
                    if age > WORKER_STALE_SECONDS
                    else ("busy" if worker.current_job_id is not None else "idle"),
                    heartbeat_age_seconds=age,
                    current_job_age_seconds=(
                        max(0, (now - worker.job_started_at).total_seconds())
                        if worker.job_started_at is not None
                        else None
                    ),
                )
            )
        certificates = self.certificates.certificate_statuses()
        return OperationalStatus(
            observed_at=now,
            queue=self.queue.statistics(now=now),
            workflows=self.workflows.statistics(),
            workers=tuple(workers),
            unreadable_worker_records=observations.unreadable,
            certificates=CertificateStatistics(
                managed=len(certificates),
                expired=sum(item.expired for item in certificates),
                expiring=sum(item.days_remaining <= 30 for item in certificates),
                renewal_due=sum(item.due_for_renewal() for item in certificates),
                next_expiry_at=min(
                    (item.not_after for item in certificates), default=None
                ),
            ),
        )
