"""The published shape of a workflow.

It was in `blitzecdn.api.models` with the frame and core's own shapes, because
the journal was core's. What a client is shown is still a decision separate
from what the domain holds — the fields are restated rather than re-exported —
so this is the same arrangement `deployments` and every installed package
already use, applied to the capability that has just stopped being core.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from blitzecdn.api.models import Model
from blitzecdn.capabilities.workflows.domain import WorkflowKind, WorkflowStatus

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
