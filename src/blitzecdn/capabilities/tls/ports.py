"""Read-only certificate reporting for other capabilities."""

from typing import Protocol

from blitzecdn.capabilities.tls.certificates.domain import (
    CertificateStatus,
    PreflightAttempt,
)


class CertificateHistory(Protocol):
    def certificate_statuses(self) -> list[CertificateStatus]:
        """Every managed certificate against the clock, soonest expiry first."""
        ...

    def preflight_attempts(self) -> list[PreflightAttempt]:
        """Sites whose last scheduled preflight blocked, and when each is next due."""
        ...
