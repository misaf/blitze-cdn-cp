import pytest
from pydantic import ValidationError

from blitzecdn.domain.models import CdnSite, SitePatch


def test_site_normalizes_safe_hostnames(site_payload):
    site_payload["server_names"] = ["CDN.Example.COM.", "*.assets.example.com"]
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
    site_payload["certificate_path"] = "/etc/nginx/tls/fullchain.pem"
    with pytest.raises(ValidationError, match="both certificate paths"):
        CdnSite.model_validate(site_payload)


def test_patch_revalidates_complete_site(site_payload):
    site = CdnSite.model_validate(site_payload)
    updated = SitePatch(origin_host="192.0.2.20", cache_enabled=False).apply(site)
    assert updated.origin_host == "192.0.2.20"
    assert updated.cache_enabled is False
