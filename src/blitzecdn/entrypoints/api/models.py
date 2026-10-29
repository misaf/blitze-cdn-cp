"""The representation frame every HTTP model uses, and core's own shapes.

:class:`Model` and :func:`as_operation` are the frame; ``HostRun``, the audit
entry and the queued ``Operation`` are core's. Every other representation
lives in its capability's ``api.models``, and a capability's shape stays its
own even when another returns it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from blitzecdn.core.domain.runs import RunStatus, TaskOutcome


class Model(BaseModel):
    """The base of every HTTP representation: unknown fields refused."""

    model_config = ConfigDict(extra="forbid", json_schema_mode_override="validation")


class TaskResult(Model):
    task: str
    action: str = ""
    outcome: TaskOutcome
    message: str | None = None
    role: str | None = None


class HostRun(Model):
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


class AnsibleRun(Model):
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


class AuditEvent(Model):
    id: int
    created_at: datetime
    operator: str
    action: str
    resource_type: str
    resource_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class Operation(Model):
    """Work queued for the worker; poll ``GET /v3/operations/{id}`` for it.

    ``succeeded`` means the operation ran; ``result`` is the report the
    synchronous command prints, per edge, and may itself record a partial
    outcome. ``failed`` carries ``error`` and no result.
    """

    id: str
    kind: str
    status: Literal["pending", "running", "succeeded", "failed"]
    created_at: datetime
    finished_at: datetime | None = None
    error: str | None = None
    result: dict[str, Any] | None = None


def as_queued(job: Any) -> Operation:
    """Represent a queued job as the operation a client polls."""
    return Operation(
        id=job.id,
        kind=job.kind,
        status=job.status,
        created_at=job.created_at,
        finished_at=job.finished_at,
        error=job.last_error,
        result=job.result,
    )


def as_operation[T: BaseModel](model: object, target: type[T]) -> T:
    """Map a domain model to its explicit HTTP representation."""
    if hasattr(model, "model_dump"):
        return target.model_validate(model.model_dump(mode="json"))
    return target.model_validate(model)


__all__ = [
    "AnsibleRun",
    "AuditEvent",
    "HostRun",
    "Model",
    "Operation",
    "TaskResult",
    "as_operation",
    "as_queued",
]
