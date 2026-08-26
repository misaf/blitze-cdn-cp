"""Read-side certificate use cases."""

from datetime import UTC, datetime

from blitzecdn.application.ports.certificates import CertificateStore
from blitzecdn.application.ports.dns import SiteStore
from blitzecdn.domain.certificates import (
    CERTIFICATE_RENEWAL_DAYS,
    CertificateInfo,
    CertificateStatus,
)


class CertificateQueries:
    def __init__(self, *, sites: SiteStore, certificates: CertificateStore) -> None:
        self._sites = sites
        self._certificates = certificates

    def get(self, name: str) -> CertificateInfo:
        self._sites.get_site(name)
        return self._certificates.get(name)

    def statuses(self) -> list[CertificateStatus]:
        now = datetime.now(UTC)
        return [
            CertificateStatus.of(info, now=now)
            for info in self._certificates.list_all()
        ]

    def expiring(
        self, within_days: int = CERTIFICATE_RENEWAL_DAYS
    ) -> list[CertificateStatus]:
        return [
            status for status in self.statuses() if status.days_remaining <= within_days
        ]
