"""What we hold, and what is checked before we ask for more.

Two modules with nothing between them: `certificate.py` is the material and the
requests that change it, `preflight.py` is the report on DNS, reachability and
CAA that decides whether asking is worth it. Neither imports the other, which
is why one file holding both was one file holding two subjects.
"""

from blitzecdn_certificates.certificates.domain.certificate import (
    CERTIFICATE_RENEWAL_DAYS,
    CERTIFICATE_WORKFLOW,
    CertificateInfo,
    CertificateRequest,
    CertificateSource,
    CertificateStatus,
    ReconciliationResult,
    RenewalResult,
)
from blitzecdn_certificates.certificates.domain.preflight import (
    TTL_CUTOVER_ADVISORY_SECONDS,
    PreflightCheck,
    PreflightReport,
    PreflightSeverity,
)

__all__ = [
    "CERTIFICATE_RENEWAL_DAYS",
    "CERTIFICATE_WORKFLOW",
    "TTL_CUTOVER_ADVISORY_SECONDS",
    "CertificateInfo",
    "CertificateRequest",
    "CertificateSource",
    "CertificateStatus",
    "PreflightCheck",
    "PreflightReport",
    "PreflightSeverity",
    "ReconciliationResult",
    "RenewalResult",
]
