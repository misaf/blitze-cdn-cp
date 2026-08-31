"""Operational representations shared by every version of the HTTP API.

A resource representation is versioned because its *shape* is a promise to
clients: `CdnSite` gained fields in v2, so `v1_models` and `v2_models` describe
it separately and `v1_models.V1Model._project` keeps the frozen version honest.
These models are different in kind. They describe what an *operation* did — a
run of Ansible, a queued deployment, a purge, a drift check, a workflow — and
both published versions have always described it identically, character for
character. The two files that said so were maintained by copying one into the
other, which is not a version boundary; it is the same contract written twice,
where a fix applied to one and forgotten in the other is invisible.

So the shape lives here once, and both versions serve it.

**How a version diverges from this.** It does not edit these classes. The
version that needs the new field defines its own class, in its own module, with
the version in the name — exactly what `v2_models.CdnSiteV2` does and for the
same reason: FastAPI names a published component after the class, and pydantic
disambiguates a collision by qualifying *both* sides with their module path, so
a second `Deployment` would rename the other version's schema as a side effect.
That version's routes then bind to the new class and the other version keeps
pointing here, unchanged and unbroken.

`tests/api/test_common.py` pins the shape each version actually publishes, so
an edit here that changed one of them fails as a contract break rather than
shipping as a shared-code cleanup.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from blitzecdn.core.operations import WorkflowKind, WorkflowStatus
from blitzecdn.core.runs import RunStatus, TaskOutcome
from blitzecdn.features.deployments.domain import DeploymentStatus
from blitzecdn.features.http.policy import HttpScheme
from blitzecdn.features.tls.certificates.domain import (
    CertificateRequest as DomainCertificateRequest,
)
from blitzecdn.features.tls.certificates.domain import (
    CertificateSource,
    PreflightSeverity,
)
from blitzecdn.features.tls.policy import SslMode


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


class CertificateInfo(OperationModel):
    site: str
    source: CertificateSource
    domains: tuple[str, ...]
    not_before: datetime
    not_after: datetime
    fingerprint_sha256: str
    email: str | None = None


class CertificateStatus(OperationModel):
    site: str
    source: CertificateSource
    domains: tuple[str, ...]
    not_after: datetime
    days_remaining: int
    expired: bool
    renewable: bool
    fingerprint_sha256: str


class PreflightCheck(OperationModel):
    name: str
    passed: bool
    severity: PreflightSeverity
    detail: str


class PreflightReport(OperationModel):
    site: str
    checks: tuple[PreflightCheck, ...]


class CertificateRequest(OperationModel):
    email: str | None = Field(default=None, max_length=254)
    skip_preflight: bool = False

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str | None) -> str | None:
        return DomainCertificateRequest(email=value).email


class RenewalResult(OperationModel):
    renewed: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()


class ReconciliationResult(OperationModel):
    issued: tuple[str, ...] = ()
    skipped: dict[str, str] = Field(default_factory=dict)
    failed: dict[str, str] = Field(default_factory=dict)
    deployment: Deployment | None = None


class OriginCheck(OperationModel):
    site: str
    origin: str
    scheme: HttpScheme
    ssl_mode: SslMode
    sni: str | None = None
    reachable: bool = False
    tls_verified: bool | None = None
    status: int | None = None
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    detail: str | None = None


class EdgeOriginChecks(OperationModel):
    host: str
    checked_at: datetime | None = None
    checks: tuple[OriginCheck, ...] = ()
    error: str | None = None


class OriginReport(OperationModel):
    checked_at: datetime
    host_limit: str | None = None
    edges: tuple[EdgeOriginChecks, ...] = ()


class AuditEvent(OperationModel):
    id: int
    created_at: datetime
    operator: str
    action: str
    resource_type: str
    resource_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class SslAutomaticReconciliation(OperationModel):
    scanned: tuple[str, ...] = ()
    upgraded: dict[str, SslMode] = Field(default_factory=dict)
    skipped: dict[str, str] = Field(default_factory=dict)
    deployment: Deployment | None = None


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
    "CertificateInfo",
    "CertificateRequest",
    "CertificateStatus",
    "Deployment",
    "DriftReport",
    "EdgeOriginChecks",
    "EdgeRemoval",
    "HostRun",
    "OperationModel",
    "OriginCheck",
    "OriginReport",
    "PreflightCheck",
    "PreflightReport",
    "ReconciliationResult",
    "RenewalResult",
    "SslAutomaticReconciliation",
    "TaskResult",
    "Workflow",
    "WorkflowStep",
    "as_operation",
]
