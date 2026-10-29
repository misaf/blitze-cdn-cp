"""Keep HTTP creation defaults aligned with the policy and command contracts."""

import pytest
from fastapi.testclient import TestClient
from support.control_plane import API_HEADERS, control_plane_app

from blitzecdn.capabilities.cache.api.models import PurgeEntry as ApiPurgeEntry
from blitzecdn.capabilities.cache.domain import PurgeEntry
from blitzecdn.capabilities.dns.api.models import DnsRecord as ApiDnsRecord
from blitzecdn.capabilities.dns.api.models import Domain as ApiDomain
from blitzecdn.capabilities.dns.api.models import RecordCreate, RuleCreate
from blitzecdn.capabilities.dns.domain import DnsRecord, Rule
from blitzecdn.capabilities.edges.api.models import Edge as ApiEdge
from blitzecdn.capabilities.edges.domain import Edge
from blitzecdn.capabilities.serving.contract import SitePolicy
from blitzecdn.capabilities.tls.api.models import (
    CertificateRequest as ApiCertificateRequest,
)
from blitzecdn.capabilities.tls.certificates.domain import CertificateRequest


@pytest.mark.parametrize(
    ("api", "domain", "required"),
    [
        (
            ApiDnsRecord,
            DnsRecord,
            {"domain": "example.com", "name": "www", "value": "192.0.2.10"},
        ),
        (ApiEdge, Edge, {"name": "edge-1", "host": "192.0.2.20"}),
        (ApiPurgeEntry, PurgeEntry, {"host": "example.com", "uri": "/"}),
        (ApiCertificateRequest, CertificateRequest, {}),
    ],
)
def test_creation_defaults_match_domain_contracts(api, domain, required):
    assert api.model_validate(required).model_dump(
        mode="json"
    ) == domain.model_validate(required).model_dump(mode="json")


def test_zone_creation_defaults_match_every_policy_component():
    assert ApiDomain(name="example.com").model_dump(
        mode="json", exclude={"name"}
    ) == SitePolicy().settings(mode="json")


def test_record_creation_defaults_match_the_domain_record():
    required = {"name": "www", "value": "192.0.2.10"}
    assert RecordCreate(**required).to_domain("example.com") == DnsRecord(
        domain="example.com", **required
    )


def test_rule_defaults_preserve_inheritance():
    required = {"name": "no-cache", "overrides": {"cache_enabled": False}}
    actual = RuleCreate(**required).to_domain("example.com")
    expected = Rule(domain="example.com", **required)
    assert actual.model_dump(mode="json") == expected.model_dump(mode="json")
    assert dict(actual.overrides) == {"cache_enabled": False}


def test_patch_preserves_omissions_and_distinguishes_null_from_defaults(settings):
    with TestClient(control_plane_app(settings)) as client:
        created = client.post(
            "/v3/domains",
            headers=API_HEADERS,
            json={
                "name": "example.com",
                "origin_sni": "origin.example.com",
                "cache_valid_success": "1h",
                "visitor_headers": {"connecting_ip": False, "ip_country": True},
            },
        )
        assert created.status_code == 201
        patched = client.patch(
            "/v3/domains/example.com",
            headers=API_HEADERS,
            json={"origin_sni": None, "visitor_headers": {"connecting_ip": True}},
        )
        assert patched.status_code == 200
        assert patched.json()["origin_sni"] is None
        assert patched.json()["cache_valid_success"] == "1h"
        assert patched.json()["visitor_headers"] == {
            "connecting_ip": True,
            "ip_country": False,
        }
        rejected = client.patch(
            "/v3/domains/example.com", headers=API_HEADERS, json={"cache_enabled": None}
        )
        assert rejected.status_code == 422
        current = client.get("/v3/domains/example.com", headers=API_HEADERS)
        assert current.json() == patched.json()
