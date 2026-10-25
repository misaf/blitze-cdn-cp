"""The published shape of a workflow, restated from the domain model."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from blitzecdn.capabilities.workflows.domain import WorkflowKind, WorkflowStatus
from blitzecdn.entrypoints.api.models import Model

__all__ = ["Workflow", "WorkflowStep"]


class WorkflowStep(Model):
    name: str
    completed_at: datetime
    details: dict[str, Any] = Field(default_factory=dict)


class Workflow(Model):
    id: str
    kind: WorkflowKind
    resource_id: str | None = None
    status: WorkflowStatus
    operator: str
    created_at: datetime
    updated_at: datetime
    steps: tuple[WorkflowStep, ...] = ()
    error: str | None = None
