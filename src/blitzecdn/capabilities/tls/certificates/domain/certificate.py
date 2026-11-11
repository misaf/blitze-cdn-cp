"""The material we hold for a site, and the requests that change it."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    EmailStr,
    Field,
)

from blitzecdn.capabilities.deployments import Deployment
from blitzecdn.capabilities.tls.contract import CertificateVersion
from blitzecdn.capabilities.workflows import WorkflowKind

#: The journal entry an issuance opens, named by the capability that opens it.
CERTIFICATE_WORKFLOW: WorkflowKind = "certificate"


class CertificateSource(StrEnum):
    UPLOADED = "uploaded"
    ACME = "acme"


class CertificateInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    site: str
    source: CertificateSource
    domains: tuple[str, ...]
    not_before: datetime
    not_after: datetime
    fingerprint_sha256: str
    certificate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    private_key_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    email: str | None = None

    @property
    def version(self) -> CertificateVersion:
        return CertificateVersion(
            certificate_digest=self.certificate_digest,
            private_key_digest=self.private_key_digest,
        )


#: Renew ACME certificates with this many days left: Let's Encrypt's 30 of 90,
#: leaving two more windows to retry before expiry.
CERTIFICATE_RENEWAL_DAYS = 30


class CertificateStatus(BaseModel):
    """A stored certificate seen against the clock.

    Derived on request, apart from ``CertificateInfo``, whose ``metadata.json`` is
    read back with ``extra="forbid"``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    site: str
    source: CertificateSource
    domains: tuple[str, ...]
    not_after: datetime
    #: Whole days until expiry, negative once it has passed.
    days_remaining: int
    expired: bool
    #: Whether BlitzeCDN can renew this one itself. Only ACME certificates
    #: qualify; an uploaded one has to be replaced by whoever supplied it.
    renewable: bool
    fingerprint_sha256: str

    @classmethod
    def of(cls, info: CertificateInfo, *, now: datetime) -> Self:
        remaining = info.not_after - now
        return cls(
            site=info.site,
            source=info.source,
            domains=info.domains,
            not_after=info.not_after,
            # Truncated toward minus infinity, so a certificate with six hours
            # left reports 0 rather than rounding up to a reassuring 1.
            days_remaining=remaining.days,
            expired=remaining.total_seconds() <= 0,
            renewable=info.source is CertificateSource.ACME,
            fingerprint_sha256=info.fingerprint_sha256,
        )

    def due_for_renewal(self, within_days: int = CERTIFICATE_RENEWAL_DAYS) -> bool:
        return self.renewable and self.days_remaining <= within_days


def _strip(value: object) -> object:
    return value.strip() if isinstance(value, str) else value


#: An ACME account address. ``email-validator`` (through Pydantic's ``EmailStr``)
#: owns the grammar, length limits and domain checks; no deliverability lookup is
#: made. Stored lower-cased so the same account is not registered twice.
AcmeEmail = Annotated[
    EmailStr, BeforeValidator(_strip), AfterValidator(lambda value: value.lower())
]


class CertificateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: AcmeEmail | None = None
    #: Issue despite a blocking preflight, for what the controller cannot see —
    #: split-horizon DNS, an edge behind an unlisted address. Usually it just spends
    #: a rate limit.
    skip_preflight: bool = False


class RenewalResult(BaseModel):
    """The outcome of one renewal sweep, per site.

    ``failed`` needs a person; ``skipped`` — budget spent, lock held, or not
    reissuable by us — is picked up by the next run.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    renewed: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failed


class ReconciliationResult(BaseModel):
    """First certificates issued, and the queued deployment installing them, if any."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    issued: tuple[str, ...] = ()
    #: Site name to the reason it was passed over — a blocking preflight, most
    #: often, which is a fact about DNS rather than an error.
    skipped: dict[str, str] = Field(default_factory=dict)
    #: Site name to the reason issuance failed.
    failed: dict[str, str] = Field(default_factory=dict)
    deployment: Deployment | None = None

    @property
    def ok(self) -> bool:
        return not self.failed
