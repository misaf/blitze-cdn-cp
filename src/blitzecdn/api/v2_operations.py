"""Version 2 control-plane representations for operational resources."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from pydantic import Field, field_validator

from blitzecdn.api.v2_models import V2Model
from blitzecdn.domain.cache import PurgeEntry as DomainPurgeEntry
from blitzecdn.domain.certificates import CertificateRequest as DomainCertificateRequest
from blitzecdn.domain.certificates import (
    CertificateSource,
    PreflightSeverity,
)
from blitzecdn.domain.deployments import DeploymentStatus
from blitzecdn.domain.operations import WorkflowKind, WorkflowStatus
from blitzecdn.domain.runs import RunStatus, TaskOutcome
from blitzecdn.domain.sites import HttpScheme, SslMode


class TaskResult(V2Model):
    task: str
    action: str = ""
    outcome: TaskOutcome
    message: str | None = None
    role: str | None = None


class HostRun(V2Model):
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


class AnsibleRun(V2Model):
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


class Deployment(V2Model):
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


class DriftReport(V2Model):
    deployment_id: str
    checked_at: datetime
    host_limit: str | None = None
    hosts: tuple[HostRun, ...] = ()
    unattempted: tuple[str, ...] = ()


class WorkflowStep(V2Model):
    name: str
    completed_at: datetime
    details: dict[str, Any] = Field(default_factory=dict)


class Workflow(V2Model):
    id: str
    kind: WorkflowKind
    resource_id: str | None = None
    status: WorkflowStatus
    operator: str
    created_at: datetime
    updated_at: datetime
    steps: tuple[WorkflowStep, ...] = ()
    error: str | None = None


class CertificateInfo(V2Model):
    site: str
    source: CertificateSource
    domains: tuple[str, ...]
    not_before: datetime
    not_after: datetime
    fingerprint_sha256: str
    email: str | None = None


class CertificateStatus(V2Model):
    site: str
    source: CertificateSource
    domains: tuple[str, ...]
    not_after: datetime
    days_remaining: int
    expired: bool
    renewable: bool
    fingerprint_sha256: str


class PreflightCheck(V2Model):
    name: str
    passed: bool
    severity: PreflightSeverity
    detail: str


class PreflightReport(V2Model):
    site: str
    checks: tuple[PreflightCheck, ...]


class CertificateRequest(V2Model):
    email: str | None = Field(default=None, max_length=254)
    skip_preflight: bool = False

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str | None) -> str | None:
        return DomainCertificateRequest(email=value).email


class RenewalResult(V2Model):
    renewed: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()


class ReconciliationResult(V2Model):
    issued: tuple[str, ...] = ()
    skipped: dict[str, str] = Field(default_factory=dict)
    failed: dict[str, str] = Field(default_factory=dict)
    deployment: Deployment | None = None


class PurgeEntry(V2Model):
    host: str
    uri: str
    scheme: HttpScheme = HttpScheme.HTTPS

    @field_validator("host", "uri")
    @classmethod
    def valid_entry_field(cls, value: str, info: Any) -> str:
        payload = {"host": "example.com", "uri": "/", info.field_name: value}
        return cast(
            "str", getattr(DomainPurgeEntry.model_validate(payload), info.field_name)
        )

    def to_domain(self) -> DomainPurgeEntry:
        return DomainPurgeEntry.model_validate(self.model_dump())


class PurgeResult(V2Model):
    purged_at: datetime
    entries: tuple[PurgeEntry, ...] = ()
    purge_all: bool = False
    host_limit: str | None = None
    hosts: tuple[HostRun, ...] = ()
    complete: bool
    failed_hosts: tuple[str, ...]


class SiteCacheStats(V2Model):
    site: str
    outcomes: dict[str, int] = Field(default_factory=dict)


class EdgeStats(V2Model):
    host: str
    collected_at: datetime | None = None
    nginx_reachable: bool = False
    connections: dict[str, int] = Field(default_factory=dict)
    sites: tuple[SiteCacheStats, ...] = ()
    error: str | None = None


class CacheStatsReport(V2Model):
    collected_at: datetime
    host_limit: str | None = None
    edges: tuple[EdgeStats, ...] = ()


class OriginCheck(V2Model):
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


class EdgeOriginChecks(V2Model):
    host: str
    checked_at: datetime | None = None
    checks: tuple[OriginCheck, ...] = ()
    error: str | None = None


class OriginReport(V2Model):
    checked_at: datetime
    host_limit: str | None = None
    edges: tuple[EdgeOriginChecks, ...] = ()


class AuditEvent(V2Model):
    id: int
    created_at: datetime
    operator: str
    action: str
    resource_type: str
    resource_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class SslAutomaticReconciliation(V2Model):
    scanned: tuple[str, ...] = ()
    upgraded: dict[str, SslMode] = Field(default_factory=dict)
    skipped: dict[str, str] = Field(default_factory=dict)
    deployment: Deployment | None = None


class EdgeRemoval(V2Model):
    name: str
    decommissioned: bool
    hosts: tuple[HostRun, ...] = ()


def as_v2[T: V2Model](model: object, target: type[T]) -> T:
    """Map a domain model to its explicit HTTP representation."""
    if hasattr(model, "model_dump"):
        return target.model_validate(model.model_dump(mode="json"))
    return target.model_validate(model)


__all__ = [
    "AuditEvent",
    "CacheStatsReport",
    "CertificateInfo",
    "CertificateRequest",
    "CertificateStatus",
    "Deployment",
    "DriftReport",
    "EdgeRemoval",
    "HostRun",
    "OriginReport",
    "PreflightReport",
    "PurgeEntry",
    "PurgeResult",
    "ReconciliationResult",
    "RenewalResult",
    "SslAutomaticReconciliation",
    "Workflow",
    "as_v2",
]
