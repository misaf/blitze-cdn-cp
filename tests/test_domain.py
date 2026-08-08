from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from blitzecdn.domain.models import (
    CdnSite,
    CertificateInfo,
    CertificateSource,
    CertificateStatus,
    DnsRecord,
    RecordPatch,
    SitePolicy,
)


def test_site_normalizes_safe_hostnames(site_payload):
    site_payload["server_names"] = ["CDN.Example.COM.", "*.assets.example.com"]
    site_payload["origin_host"] = "Origin.Example.COM."
    site = CdnSite.model_validate(site_payload)
    assert site.server_names == ("cdn.example.com", "*.assets.example.com")
    assert site.origin_host == "origin.example.com"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "../site"),
        ("server_names", ["example.com; return 200"]),
        ("origin_host", "origin.example.com/path"),
        ("cache_valid_success", "10 minutes"),
        ("certificate_path", "../../secret"),
    ],
)
def test_site_rejects_injection_and_path_traversal(site_payload, field, value):
    site_payload[field] = value
    with pytest.raises(ValidationError):
        CdnSite.model_validate(site_payload)


def test_existing_certificate_requires_complete_pair(site_payload):
    site_payload["certificate_mode"] = "existing"
    site_payload["certificate_path"] = "/etc/ssl/example/fullchain.pem"
    with pytest.raises(ValidationError, match="both certificate paths"):
        CdnSite.model_validate(site_payload)


@pytest.mark.parametrize(
    "path",
    [
        "/etc/cron.d/blitzecdn",
        "/root/.ssh/authorized_keys",
        "/etc/nginx/conf.d/evil.conf",
        "/var/lib/blitzecdn/tls/fullchain.pem",
    ],
)
def test_certificate_paths_stay_inside_certificate_directories(site_payload, path):
    """Deploys copy these paths as root, so they must not escape TLS directories."""
    site_payload["certificate_mode"] = "existing"
    site_payload["certificate_path"] = path
    site_payload["certificate_key_path"] = "/etc/ssl/example/privkey.pem"
    with pytest.raises(ValidationError, match="must live under"):
        CdnSite.model_validate(site_payload)


def test_managed_certificate_modes_reject_operator_chosen_paths(site_payload):
    site_payload["certificate_mode"] = "uploaded"
    site_payload["certificate_path"] = "/etc/ssl/elsewhere/fullchain.pem"
    site_payload["certificate_key_path"] = "/etc/ssl/elsewhere/privkey.pem"
    with pytest.raises(ValidationError, match="upload and request endpoints"):
        CdnSite.model_validate(site_payload)


def test_managed_certificate_modes_accept_their_own_paths(site_payload):
    site_payload["certificate_mode"] = "uploaded"
    site_payload["certificate_path"] = (
        "/etc/blitzecdn/tls/cdn-example-com/fullchain.pem"
    )
    site_payload["certificate_key_path"] = (
        "/etc/blitzecdn/tls/cdn-example-com/privkey.pem"
    )
    assert CdnSite.model_validate(site_payload).certificate_mode == "uploaded"


def _managed_record(**overrides: object) -> dict[str, object]:
    return {
        "domain": "example.com",
        "name": "cdn",
        "value": "198.51.100.10",
        "proxied": True,
        "certificate_mode": "uploaded",
        "certificate_path": "/etc/blitzecdn/tls/cdn-example-com/fullchain.pem",
        "certificate_key_path": "/etc/blitzecdn/tls/cdn-example-com/privkey.pem",
        **overrides,
    }


def test_record_patch_cannot_redirect_a_managed_certificate():
    """The escalation path: aim a managed record's cert at an arbitrary file.

    A deploy writes these paths as root, so the record has to be refused before
    it reaches the derived desired state.
    """
    record = DnsRecord.model_validate(_managed_record())
    patch = RecordPatch(certificate_path="/etc/cron.d/blitzecdn")
    with pytest.raises(ValidationError):
        DnsRecord.model_validate(
            {**record.model_dump(), **patch.model_dump(exclude_unset=True)}
        )


def test_record_patch_revalidates_the_whole_record():
    record = DnsRecord.model_validate(_managed_record())
    patch = RecordPatch(value="192.0.2.20", cache_enabled=False)
    updated = DnsRecord.model_validate(
        {**record.model_dump(), **patch.model_dump(exclude_unset=True)}
    )
    assert updated.value == "192.0.2.20"
    assert updated.cache_enabled is False
    assert updated.to_site().origin_host == "192.0.2.20"


def test_record_patch_covers_every_shared_policy_field():
    """`RecordPatch` cannot inherit `SitePolicy`, so nothing else keeps it honest.

    Every policy field has to be patchable. One missing is not an error anyone
    sees: the API accepts the request, silently drops the unknown key under
    `extra="forbid"`... or worse, rejects a field an operator can set on
    creation but never change. Adding a field to `SitePolicy` should fail here
    until it is added below it too.
    """
    assert set(SitePolicy.model_fields) <= set(RecordPatch.model_fields), (
        "RecordPatch is missing "
        f"{sorted(set(SitePolicy.model_fields) - set(RecordPatch.model_fields))}. "
        "Add the field to RecordPatch as an optional defaulting to None."
    )


def test_every_patchable_policy_field_is_optional():
    """An inherited required field would arrive here with a default and apply
    itself on every unrelated patch."""
    for name in SitePolicy.model_fields:
        assert RecordPatch.model_fields[name].default is None, (
            f"RecordPatch.{name} must default to None so an unset field means "
            "'leave alone' rather than 'reset to this value'"
        )


def test_a_site_derived_from_a_record_carries_every_policy_field():
    """`to_site()` copies the policy by name; prove nothing is lost in transit."""
    record = DnsRecord.model_validate(
        _managed_record(
            origin_port=8443, cache_valid_success="30m", origin_sni="o.test"
        )
    )
    site = record.to_site()
    for name in SitePolicy.model_fields:
        assert getattr(site, name) == getattr(record, name), (
            f"{name} did not survive DnsRecord.to_site()"
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
    days, expected_remaining, expired
):
    now = datetime.now(UTC)
    status = CertificateStatus.of(_info(days, CertificateSource.ACME), now=now)
    assert status.days_remaining == expected_remaining
    assert status.expired is expired


def test_a_certificate_with_hours_left_does_not_round_up_to_a_reassuring_day():
    """Truncating toward zero here would report 1 day for something due tonight."""
    now = datetime.now(UTC)
    info = _info(1, CertificateSource.ACME).model_copy(
        update={"not_after": now + timedelta(hours=6)}
    )
    assert CertificateStatus.of(info, now=now).days_remaining == 0


def test_only_acme_certificates_are_renewable():
    now = datetime.now(UTC)
    acme = CertificateStatus.of(_info(10, CertificateSource.ACME), now=now)
    uploaded = CertificateStatus.of(_info(10, CertificateSource.UPLOADED), now=now)

    assert acme.renewable is True
    assert acme.due_for_renewal() is True
    assert uploaded.renewable is False
    assert uploaded.due_for_renewal() is False, (
        "BlitzeCDN cannot reissue a certificate someone else supplied"
    )


def test_a_certificate_outside_the_window_is_not_due():
    now = datetime.now(UTC)
    status = CertificateStatus.of(_info(60, CertificateSource.ACME), now=now)
    assert status.due_for_renewal() is False
    assert status.due_for_renewal(within_days=90) is True
