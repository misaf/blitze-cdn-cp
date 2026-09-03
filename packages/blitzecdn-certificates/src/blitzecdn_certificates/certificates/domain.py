"""Certificates we hold, and the preflight that decides whether to ask for one."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

from blitzecdn.core.validation import hostname
from blitzecdn.capabilities.deployments.domain import Deployment


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
    email: str | None = None


#: Renew an ACME certificate once it has this many days left.
#:
#: Let's Encrypt issues for 90 days and recommends renewing at 30, which
#: leaves two further windows to retry in before anything expires. Renewal
#: goes through HTTP-01, so a failure is usually a transient DNS or reachability
#: problem that a later attempt clears.
CERTIFICATE_RENEWAL_DAYS = 30


class CertificateStatus(BaseModel):
    """A stored certificate seen against the clock.

    Separate from ``CertificateInfo`` because that is the record written to
    ``metadata.json`` and read back under ``extra="forbid"``. That file is also
    the atomic pointer to its fingerprinted chain/key bundle, so folding a
    computed field into it would make every stored file fail to reload. This is
    the derived view, built when someone asks.
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


#: A proxied record with a TTL above this is flagged before issuance.
#:
#: Not an error — a long TTL is legitimate for a hostname that has been live
#: for months. It matters only around a cutover: resolvers that cached the old
#: address keep answering with it for up to the TTL, and HTTP-01 validation
#: follows public DNS, so a slow rollover shows up as an issuance failure with
#: no obvious cause.
TTL_CUTOVER_ADVISORY_SECONDS = 3600


class PreflightSeverity(StrEnum):
    """Whether a failed check stops issuance or is merely reported.

    ``BLOCKING`` is reserved for conditions under which HTTP-01 validation
    cannot succeed, so failing early costs nothing and spares the CA's rate
    limit. Everything a certificate can legitimately be issued in spite of —
    an origin that is down, a TTL that is high — is ``ADVISORY``.
    """

    BLOCKING = "blocking"
    ADVISORY = "advisory"


class PreflightCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Stable identifier (``dns``, ``caa``, ``deployed``, ``origin``, ``ttl``),
    #: safe for a caller to branch on. ``detail`` is prose and is not.
    name: str
    passed: bool
    severity: PreflightSeverity
    detail: str


class PreflightReport(BaseModel):
    """What the outside world looks like just before we ask a CA for a cert."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    site: str
    checks: tuple[PreflightCheck, ...]

    @property
    def blocking_failures(self) -> tuple[PreflightCheck, ...]:
        return tuple(
            check
            for check in self.checks
            if not check.passed and check.severity is PreflightSeverity.BLOCKING
        )

    @property
    def advisories(self) -> tuple[PreflightCheck, ...]:
        return tuple(
            check
            for check in self.checks
            if not check.passed and check.severity is PreflightSeverity.ADVISORY
        )

    @property
    def ok(self) -> bool:
        """True when nothing blocks issuance. Advisories do not count."""
        return not self.blocking_failures

    def summary(self) -> str:
        return "; ".join(
            f"{check.name}: {check.detail}" for check in self.blocking_failures
        )


class CertificateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str | None = Field(default=None, max_length=254)
    #: Issue even though preflight found a blocking problem. Deliberately not
    #: the default: every blocking check describes a condition under which the
    #: CA cannot validate, so the usual effect of overriding is to spend a rate
    #: limit on a request that fails anyway. It exists for the case the
    #: controller cannot see — split-horizon DNS, or an edge fronted by an
    #: address the inventory does not name.
    skip_preflight: bool = False

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip().lower()
        if candidate.count("@") != 1 or any(char.isspace() for char in candidate):
            raise ValueError("email must be a valid address")
        local, domain = candidate.rsplit("@", 1)
        if not local or not domain:
            raise ValueError("email must be a valid address")
        hostname(domain)
        return candidate


class RenewalResult(BaseModel):
    """The outcome of one renewal sweep, per site.

    Three buckets rather than two, because "not renewed" has two very different
    causes and only one of them needs a person. ``failed`` is a site the CA or
    preflight refused; ``skipped`` is a site this run did not get to — the
    budget ran out, the deployment lock was held, or the certificate is one
    BlitzeCDN cannot reissue. A skipped site is picked up by the next run, so a
    sweep full of them is slower renewal rather than missed renewal.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    renewed: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failed


class ReconciliationResult(BaseModel):
    """First certificates issued for sites that were ready, and the deploy.

    ``deployment`` is ``None`` when nothing was issued: reconciliation only
    converges the fleet after it has something new to install, so a run that
    found no eligible site changes nothing at all.
    """

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
