"""Read-only queue aggregates and cross-process worker progress."""

from datetime import datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from blitzecdn.capabilities.jobs.domain.job import JobStatus


class QueueStatistics(BaseModel):
    model_config = ConfigDict(frozen=True)
    counts: dict[JobStatus, int] = Field(default_factory=dict)
    ready: int = 0
    expired_leases: int = 0
    oldest_ready_age_seconds: float = 0


class WorkerHeartbeat(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    worker_id: str
    heartbeat_at: AwareDatetime
    current_job_id: str | None = None
    current_kind: str | None = None
    job_started_at: AwareDatetime | None = None
    last_finished_at: AwareDatetime | None = None
    last_duration_seconds: float | None = Field(default=None, ge=0)

    def age_seconds(self, now: datetime) -> float:
        return max(0, (now - self.heartbeat_at).total_seconds())


class WorkerObservations(BaseModel):
    model_config = ConfigDict(frozen=True)
    workers: tuple[WorkerHeartbeat, ...] = ()
    unreadable: int = 0
