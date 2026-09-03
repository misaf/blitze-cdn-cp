"""Operational representations published by the HTTP API.

These describe what an *operation* did — a run of Ansible, a queued deployment,
a purge, a drift check, a workflow — as opposed to the resources in
:mod:`blitzecdn.api.models`, which describe what the control plane *holds*.

The split is worth keeping even with one published version, because the two
kinds answer to different owners. A resource shape is `sites`' or `dns`' to
change; an operational shape belongs to whatever ran the operation, and an
optional capability's operational shapes are its own — `PurgeResult` and
`CacheStatsReport` live in `blitzecdn_cache.api.models` and build on
`OperationModel` and `as_operation` here, because core cannot carry a shape for
a capability that may not be installed.

`tests/api/test_common.py` pins the shape this actually publishes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from blitzecdn.core.operations import WorkflowKind, WorkflowStatus
from blitzecdn.core.runs import RunStatus, TaskOutcome
from blitzecdn.capabilities.deployments.domain import DeploymentStatus


class OperationModel(BaseModel):
    """The base every operational representation shares.

    Same configuration the versioned representation bases carry: unknown fields
    are refused rather than ignored, and one schema is published per model
    instead of a validation/serialization pair.
    """

    model_config = ConfigDict(extra="forbid", json_schema_mode_override="validation")


class TaskResult(OperationModel):
    task: str
    action: str = ""
    outcome: TaskOutcome
    message: str | None = None
    role: str | None = None


class HostRun(OperationModel):
    host: str
    ok: int = 0
    changed: int = 0
    failed: int = 0
    unreachable: int = 0
    skipped: int = 0
    rescued: int = 0
    ignored: int = 0
    changes: tuple[TaskResult, ...] = ()
    failures: tuple[TaskResult, ...] = ()
    report: dict[str, object] | None = None


class AnsibleRun(OperationModel):
    id: str
    playbook: str
    status: RunStatus
    return_code: int | None = None
    started_at: datetime
    finished_at: datetime
    hosts: tuple[HostRun, ...] = ()
    targeted: tuple[str, ...] = ()
    log_path: str | None = None
    error: str | None = None


class Deployment(OperationModel):
    id: str
    status: DeploymentStatus
    operator: str
    check_mode: bool
    host_limit: str | None = None
    rollback_of: str | None = None
    canonical_digest: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: AnsibleRun | None = None


class DriftReport(OperationModel):
    deployment_id: str
    checked_at: datetime
    host_limit: str | None = None
    hosts: tuple[HostRun, ...] = ()
    unattempted: tuple[str, ...] = ()


class WorkflowStep(OperationModel):
    name: str
    completed_at: datetime
    details: dict[str, Any] = Field(default_factory=dict)


class Workflow(OperationModel):
    id: str
    kind: WorkflowKind
    resource_id: str | None = None
    status: WorkflowStatus
    operator: str
    created_at: datetime
    updated_at: datetime
    steps: tuple[WorkflowStep, ...] = ()
    error: str | None = None


class AuditEvent(OperationModel):
    id: int
    created_at: datetime
    operator: str
    action: str
    resource_type: str
    resource_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class EdgeRemoval(OperationModel):
    name: str
    decommissioned: bool
    hosts: tuple[HostRun, ...] = ()


def as_operation[T: BaseModel](model: object, target: type[T]) -> T:
    """Map a domain model to its explicit HTTP representation."""
    if hasattr(model, "model_dump"):
        return target.model_validate(model.model_dump(mode="json"))
    return target.model_validate(model)


__all__ = [
    "AnsibleRun",
    "AuditEvent",
    "Deployment",
    "DriftReport",
    "EdgeRemoval",
    "HostRun",
    "OperationModel",
    "TaskResult",
    "Workflow",
    "WorkflowStep",
    "as_operation",
]
