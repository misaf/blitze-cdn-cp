"""The published shape of a workflow.

Here rather than in `blitzecdn.api.models` with the frame and core's own
shapes, because the journal is this capability's. What a client is shown stays
a decision separate from what the domain holds — the fields are restated rather
than re-exported — which is the arrangement `deployments` and every installed
package use.
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
