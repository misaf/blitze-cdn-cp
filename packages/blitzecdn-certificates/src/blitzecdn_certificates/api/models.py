"""HTTP representations owned by the certificate capability."""

from datetime import datetime

from pydantic import Field, field_validator

from blitzecdn.api.operations import Deployment, OperationModel
from blitzecdn.features.tls.policy import SslMode
from blitzecdn_certificates.certificates.domain import (
    CERTIFICATE_RENEWAL_DAYS,
    CertificateSource,
    PreflightSeverity,
)
from blitzecdn_certificates.certificates.domain import (
    CertificateRequest as DomainCertificateRequest,
)


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


class RenewRequest(OperationModel):
    within_days: int = Field(default=CERTIFICATE_RENEWAL_DAYS, ge=0, le=3650)
    force: bool = False
    sites: list[str] | None = Field(default=None, min_length=1)


class RenewalResult(OperationModel):
    renewed: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()


class ReconciliationResult(OperationModel):
    issued: tuple[str, ...] = ()
    skipped: dict[str, str] = Field(default_factory=dict)
    failed: dict[str, str] = Field(default_factory=dict)
    deployment: Deployment | None = None


class SslAutomaticReconciliation(OperationModel):
    scanned: tuple[str, ...] = ()
    upgraded: dict[str, SslMode] = Field(default_factory=dict)
    skipped: dict[str, str] = Field(default_factory=dict)
    deployment: Deployment | None = None
