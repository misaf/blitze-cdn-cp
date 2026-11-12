"""The published shape of a workflow, restated from the domain model."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, model_validator

from blitzecdn.capabilities.workflows.domain import WorkflowKind, WorkflowStatus
from blitzecdn.delivery.api.models import Model, QueuedWork

__all__ = ["Workflow", "WorkflowStep"]


class WorkflowStep(Model):
    name: str
    completed_at: datetime
    details: dict[str, Any] = Field(default_factory=dict)


class Workflow(QueuedWork):
    """Crash-visible progress for work that crosses transaction systems.

    ``needs_review`` and ``reviewed`` are finished too: nothing runs until an
    operator looks. ``finished_at`` is when it stopped, and a workflow keeps
    its progress in ``steps`` rather than a ``result``.
    """

    kind: WorkflowKind
    resource_id: str | None = None
    status: WorkflowStatus
    operator: str
    updated_at: datetime
    steps: tuple[WorkflowStep, ...] = ()
    result: None = None

    @model_validator(mode="before")
    @classmethod
    def _derive_finished_at(cls, data: Any) -> Any:
        if isinstance(data, dict) and "finished_at" not in data:
            active = str(data.get("status")) in cls.ACTIVE_STATUSES
            return {**data, "finished_at": None if active else data.get("updated_at")}
        return data
