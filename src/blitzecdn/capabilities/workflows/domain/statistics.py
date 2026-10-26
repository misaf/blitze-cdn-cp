"""Aggregates over retained workflow history, not lifetime counters."""

from pydantic import BaseModel, ConfigDict, Field

from blitzecdn.capabilities.workflows.domain.workflow import WorkflowStatus


class WorkflowStatistics(BaseModel):
    model_config = ConfigDict(frozen=True)
    counts: dict[WorkflowStatus, int] = Field(default_factory=dict)
    completed_count: int = 0
    duration_seconds_sum: float = 0
