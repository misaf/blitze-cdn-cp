"""The representation vocabulary core owns, and nothing a capability owns.

Two kinds of thing live here, and the second is the rule for the first.

The **frame** — :class:`Model` and :func:`as_operation` — is what every HTTP
representation in this workspace is built from, core's own and an installed
package's alike. The **shapes** are the representations of what *core* holds:
an Ansible run and an audit entry. Each has its domain model in
``blitzecdn.core``, so core is where the published form of it belongs too.

Every other representation lives beside the routes that return it, in its
capability's ``api.models``, so adding a field to a capability's contract never
means remembering a shared module no capability owns.

What survives the split is what more than one capability answers with.
`HostRun` is reported by `edges`, by `cache` and by `origins`; `Deployment` is
returned by `deployments` and by `certificates`. The first is core's, because
an Ansible run is core's. The second is `deployments`', and `certificates`
imports it from there — a capability's shape stays that capability's even when
someone else publishes it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from blitzecdn.core.domain.runs import RunStatus, TaskOutcome


class Model(BaseModel):
    """The base every HTTP representation shares.

    Unknown fields are refused rather than ignored, and one schema is published
    per model instead of a validation/serialization pair.

    One base for resources and operational shapes alike: a site and a purge
    are both simply what their capability publishes.
    """

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
    "TaskResult",
    "as_operation",
]
