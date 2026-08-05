import pytest
from pydantic import ValidationError

from blitzecdn.domain.models import CdnSite, DnsRecord, RecordPatch


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
