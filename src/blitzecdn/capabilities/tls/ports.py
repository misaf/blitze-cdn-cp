"""Read-only certificate reporting for other capabilities."""

from typing import Protocol

from blitzecdn.capabilities.tls.certificates.domain import (
    CertificateStatus,
    PreflightAttempt,
)


class CertificateHistory(Protocol):
    def certificate_statuses(self) -> list[CertificateStatus]: ...

    def preflight_attempts(self) -> list[PreflightAttempt]: ...
