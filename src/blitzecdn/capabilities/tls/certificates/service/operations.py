"""Certificate issuance, renewal and reconciliation as queued operations.

Each waits on the CA, DNS and HTTP preflight probes, so the HTTP API queues it
for the worker instead of holding a request open; the CLI calls the service
directly and waits. A payload names the operator and the request; a run
returns the same report the synchronous call does, as JSON.
"""

from __future__ import annotations

from typing import Any, ClassVar

from blitzecdn.capabilities.jobs import JobPayload, QueuedOperation
from blitzecdn.capabilities.tls.certificates.service.issuance import (
    CertificateService,
)

__all__ = [
    "RECONCILE_DEDUPE_KEY",
    "RENEW_DEDUPE_KEY",
    "CertificateReconcileJob",
    "CertificateRenewalJob",
    "CertificateRequestJob",
    "operation_leases",
    "queued_operations",
    "request_dedupe_key",
]


class CertificateRequestJob(JobPayload):
    """Issue a certificate for one site."""

    kind: ClassVar[str] = "certificates.request"

    operator: str
    name: str
    email: str | None = None
    skip_preflight: bool = False


class CertificateRenewalJob(JobPayload):
    """Renew the certificates due within ``within_days``, or all of them."""

    kind: ClassVar[str] = "certificates.renew"

    operator: str
    within_days: int
    force: bool = False
    #: Only these sites, or every site when ``None``.
    sites: tuple[str, ...] | None = None


class CertificateReconcileJob(JobPayload):
    """Bring every site's certificate in line with its TLS settings."""

    kind: ClassVar[str] = "certificates.reconcile"

    operator: str


#: One sweep at a time: a second would only wait on the first's lock, then
#: find nothing left to do.
RENEW_DEDUPE_KEY = CertificateRenewalJob.kind
RECONCILE_DEDUPE_KEY = CertificateReconcileJob.kind


def request_dedupe_key(name: str) -> str:
    """One request per site in flight: a second would ask the CA again."""
    return f"{CertificateRequestJob.kind}:{name}"


def queued_operations(
    certificates: CertificateService, *, renewal_budget_seconds: float
) -> tuple[QueuedOperation[Any], ...]:
    """Each kind's run, from its payload to its JSON report.

    A renewal sweep stops between sites once ``renewal_budget_seconds`` has
    passed, so one job cannot outlive the worker's lease on it.
    """

    def request(job: CertificateRequestJob) -> dict[str, Any]:
        info = certificates.request_certificate(
            job.name, job.operator, job.email, skip_preflight=job.skip_preflight
        )
        return info.model_dump(mode="json")

    def renew(job: CertificateRenewalJob) -> dict[str, Any]:
        result = certificates.renew_certificates(
            job.operator,
            within_days=job.within_days,
            force=job.force,
            sites=None if job.sites is None else list(job.sites),
            budget_seconds=renewal_budget_seconds,
        )
        return result.model_dump(mode="json")

    def reconcile(job: CertificateReconcileJob) -> dict[str, Any]:
        # An operator asking now has usually just fixed something, so every
        # waiting site is checked, even one the scheduled run is backing off.
        result = certificates.reconcile_certificates(job.operator, force=True)
        return result.model_dump(mode="json")

    return (
        QueuedOperation(CertificateRequestJob, request),
        QueuedOperation(CertificateRenewalJob, renew),
        QueuedOperation(CertificateReconcileJob, reconcile),
    )


def operation_leases(
    *, renewal_budget_seconds: int, issuance_timeout_seconds: int
) -> dict[str, int]:
    """How long the worker holds each kind before another may take it over.

    One issuance is bounded by certbot's timeout. A renewal sweep checks its
    budget only between sites, so it can run one issuance past the budget,
    as the scheduled renewal's lease allows. Reconciliation is given the same
    room; it has no budget of its own.
    """
    sweep = renewal_budget_seconds + issuance_timeout_seconds
    return {
        CertificateRequestJob.kind: issuance_timeout_seconds,
        CertificateRenewalJob.kind: sweep,
        CertificateReconcileJob.kind: sweep,
    }
