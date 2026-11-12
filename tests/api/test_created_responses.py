"""Every 201 points Location at the resource it created, as every 202 does."""

import pytest
from fastapi.testclient import TestClient
from support.control_plane import API_HEADERS, control_plane_app

#: (route, body, the Location the created resource is read back from).
CREATIONS = [
    ("/v3/domains", {"name": "example.net"}, "/v3/domains/example.net"),
    (
        "/v3/domains/example.com/records",
        {"name": "www", "type": "AAAA", "value": "2001:db8::10", "proxied": False},
        "/v3/domains/example.com/records/AAAA/www",
    ),
    (
        "/v3/domains/example.com/rules",
        {
            "name": "assets",
            "match": "assets.example.com",
            "overrides": {"cache_enabled": False},
        },
        "/v3/domains/example.com/rules/assets",
    ),
    (
        "/v3/edges",
        {"name": "edge-a", "host": "edge-a.example.com"},
        "/v3/edges/edge-a",
    ),
]


@pytest.mark.parametrize(("route", "body", "location"), CREATIONS)
def test_created_resources_are_found_at_their_location(settings, route, body, location):
    with TestClient(control_plane_app(settings)) as client:
        if route != "/v3/domains":
            assert (
                client.post(
                    "/v3/domains", json={"name": "example.com"}, headers=API_HEADERS
                ).status_code
                == 201
            )
        created = client.post(route, json=body, headers=API_HEADERS)
        assert created.status_code == 201, created.text
        assert created.headers["Location"] == location
        assert (
            created.headers["ETag"]
            == client.get(location, headers=API_HEADERS).headers["ETag"]
        )
        fetched = client.get(location, headers=API_HEADERS)
        assert fetched.status_code == 200
        assert fetched.json() == created.json()


def test_a_rejected_creation_has_no_location(settings):
    with TestClient(control_plane_app(settings)) as client:
        body = {"name": "example.net"}
        assert client.post("/v3/domains", json=body, headers=API_HEADERS).is_success
        duplicate = client.post("/v3/domains", json=body, headers=API_HEADERS)
        assert duplicate.status_code == 409
        assert "Location" not in duplicate.headers


def test_openapi_documents_location_and_etag_on_every_created_response(settings):
    document = control_plane_app(settings).openapi()
    created = []
    for path, methods in document["paths"].items():
        for method, operation in methods.items():
            response = operation.get("responses", {}).get("201")
            if response is not None:
                created.append((method.upper(), path))
                assert set(response.get("headers", {})) == {"Location", "ETag"}
    assert set(created) == {
        ("POST", route.replace("example.com", "{domain}")) for route, _, _ in CREATIONS
    }
