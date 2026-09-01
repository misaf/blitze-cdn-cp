from datetime import UTC, datetime, timedelta

import pytest
from blitzecdn_certificates.certificates.domain import (
    CertificateInfo,
    CertificateSource,
    CertificateStatus,
)


def _info(days: int, source: CertificateSource) -> CertificateInfo:
    now = datetime.now(UTC)
    return CertificateInfo(
        site="cdn-example-com",
        source=source,
        domains=("cdn.example.com",),
        not_before=now - timedelta(days=1),
        not_after=now + timedelta(days=days),
        fingerprint_sha256="ab" * 32,
    )


@pytest.mark.parametrize(
    ("days", "expected_remaining", "expired"),
    [(60, 60, False), (1, 1, False), (0, 0, False), (-1, -1, True)],
)
def test_certificate_status_counts_whole_days_and_notices_expiry(
    days: int, expected_remaining: int, expired: bool
) -> None:
    now = datetime.now(UTC)
    status = CertificateStatus.of(_info(days, CertificateSource.ACME), now=now)
    assert status.days_remaining == expected_remaining
    assert status.expired is expired


def test_a_certificate_with_hours_left_does_not_round_up_to_a_day() -> None:
    now = datetime.now(UTC)
    info = _info(1, CertificateSource.ACME).model_copy(
        update={"not_after": now + timedelta(hours=6)}
    )
    assert CertificateStatus.of(info, now=now).days_remaining == 0


def test_only_acme_certificates_are_renewable() -> None:
    now = datetime.now(UTC)
    acme = CertificateStatus.of(_info(10, CertificateSource.ACME), now=now)
    uploaded = CertificateStatus.of(_info(10, CertificateSource.UPLOADED), now=now)

    assert acme.renewable is True
    assert acme.due_for_renewal() is True
    assert uploaded.renewable is False
    assert uploaded.due_for_renewal() is False


def test_a_certificate_outside_the_window_is_not_due() -> None:
    now = datetime.now(UTC)
    status = CertificateStatus.of(_info(60, CertificateSource.ACME), now=now)
    assert status.due_for_renewal() is False
    assert status.due_for_renewal(within_days=90) is True
