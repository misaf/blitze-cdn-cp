"""Record creation uses the URL's domain and preserves the response contract."""

import pytest
from fastapi.testclient import TestClient
from support.control_plane import API_HEADERS, control_plane_app


@pytest.mark.parametrize(
    "body",
    [
        {"name": "www", "value": "192.0.2.10"},
        {
            "name": "ipv6",
            "type": "AAAA",
            "value": "2001:db8::10",
            "ttl": 60,
            "proxied": False,
        },
    ],
)
def test_record_creation_uses_the_url_and_returns_the_domain(settings, body):
    with TestClient(control_plane_app(settings)) as client:
        assert (
            client.post(
                "/v3/domains", json={"name": "example.net"}, headers=API_HEADERS
            ).status_code
            == 201
        )
        response = client.post(
            "/v3/domains/example.net/records", json=body, headers=API_HEADERS
        )
        assert response.status_code == 201
        assert response.json() == {
            "domain": "example.net",
            "type": "A",
            "ttl": 300,
            "proxied": True,
            **body,
        }
        records = client.get("/v3/domains/example.net/records", headers=API_HEADERS)
        assert records.json() == [response.json()]


@pytest.mark.parametrize(
    "extra",
    [
        {"domain": "example.com"},
        {"domain": "other.example"},
        {"value": "2001:db8::10"},
        {"ttl": 0},
        {"name": ""},
    ],
)
def test_invalid_creation_is_rejected_without_storing_a_record(settings, extra):
    with TestClient(control_plane_app(settings)) as client:
        assert (
            client.post(
                "/v3/domains", json={"name": "example.com"}, headers=API_HEADERS
            ).status_code
            == 201
        )
        response = client.post(
            "/v3/domains/example.com/records",
            json={"name": "www", "value": "192.0.2.10", **extra},
            headers=API_HEADERS,
        )
        assert response.status_code == 422
        assert (
            client.get("/v3/domains/example.com/records", headers=API_HEADERS).json()
            == []
        )


def test_openapi_separates_record_creation_from_record_responses(settings):
    document = control_plane_app(settings).openapi()
    operation = document["paths"]["/v3/domains/{domain}/records"]["post"]
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/RecordCreate"
    }
    assert operation["responses"]["201"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/DnsRecord"
    }
    schemas = document["components"]["schemas"]
    assert set(schemas["RecordCreate"]["required"]) == {"name", "value"}
    assert "domain" not in schemas["RecordCreate"]["properties"]
    assert schemas["RecordCreate"]["additionalProperties"] is False
    assert "domain" in schemas["DnsRecord"]["required"]
