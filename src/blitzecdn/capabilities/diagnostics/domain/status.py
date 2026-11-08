"""One point-in-time view; retained counts are gauges, not lifetime counters."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from blitzecdn.capabilities.deployments.domain import ConvergenceHealth
from blitzecdn.capabilities.jobs.domain.observability import (
    QueueStatistics,
    WorkerHeartbeat,
)
from blitzecdn.capabilities.workflows.domain.statistics import WorkflowStatistics

WORKER_STALE_SECONDS = 30


class WorkerStatus(WorkerHeartbeat):
    state: Literal["idle", "busy", "stale"]
    heartbeat_age_seconds: float
    current_job_age_seconds: float | None = None


class CertificateStatistics(BaseModel):
    model_config = ConfigDict(frozen=True)
    managed: int
    expired: int
    expiring: int
    renewal_due: int
    next_expiry_at: datetime | None
    #: Sites waiting for a first certificate whose last preflight blocked,
    #: typically because their hostname does not point at an edge yet.
    preflight_blocked: int
    #: When reconciliation last checked one of those sites, and when it will
    #: next check the soonest; it backs off sites that keep failing.
    preflight_last_checked_at: datetime | None
    preflight_next_check_at: datetime | None


class BackupStatistics(BaseModel):
    """The last full backup recorded on this controller, or all ``None``."""

    model_config = ConfigDict(frozen=True)
    last_completed_at: datetime | None
    last_age_seconds: float | None
    last_archive: str | None
    last_size_bytes: int | None


class OperationalStatus(BaseModel):
    model_config = ConfigDict(frozen=True)
    observed_at: datetime
    queue: QueueStatistics
    workflows: WorkflowStatistics
    workers: tuple[WorkerStatus, ...]
    unreadable_worker_records: int
    worker_stale_after_seconds: int = WORKER_STALE_SECONDS
    certificates: CertificateStatistics
    backups: BackupStatistics
    #: Applied deployments and drift checks: last outcomes and failure streaks.
    convergence: tuple[ConvergenceHealth, ...]
