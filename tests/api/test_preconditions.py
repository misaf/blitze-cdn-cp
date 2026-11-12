"""``ETag`` and ``If-Match``: an edit built from a stale read is refused, not merged.

The lost-update case from issue #371: two clients read a zone, each sends a
firewall list built from that read, and the second silently drops the first's
address. With ``If-Match`` the second gets 412 and nothing is written.
"""

import pytest
from fastapi.testclient import TestClient
from support.control_plane import API_HEADERS, control_plane_app

from blitzecdn.core.domain.preconditions import IfMatch

ZONE = "/v3/domains/example.com"
RECORD = f"{ZONE}/records/A/www"
RULE = f"{ZONE}/rules/api"
EDGE = "/v3/edges/edge-1"


@pytest.fixture
def client(settings):
    with TestClient(control_plane_app(settings)) as client:
        client.headers.update(API_HEADERS)
        yield client


@pytest.fixture
def zone(client):
    created = client.post("/v3/domains", json={"name": "example.com"})
    assert created.status_code == 201, created.text
    return created


def _ok(response, status=200):
    assert response.status_code == status, response.text
    return response


def test_reads_and_writes_carry_a_strong_tag_that_moves_with_the_resource(client, zone):
    created = zone.headers["ETag"]
    assert created.startswith('"') and not created.startswith("W/")
    assert _ok(client.get(ZONE)).headers["ETag"] == created

    patched = _ok(client.patch(ZONE, json={"http3_enabled": False}))
    assert patched.headers["ETag"] == created, "an edit that changes nothing"

    patched = _ok(client.patch(ZONE, json={"cache_enabled": False}))
    assert patched.headers["ETag"] != created
    assert _ok(client.get(ZONE)).headers["ETag"] == patched.headers["ETag"]


def test_a_stale_if_match_answers_412_and_writes_nothing(client, zone):
    read = _ok(client.get(ZONE)).headers["ETag"]
    first = {"firewall": {"deny_sources": ["192.0.2.1", "192.0.2.2"]}}
    _ok(client.patch(ZONE, json=first, headers={"If-Match": read}))

    second = {"firewall": {"deny_sources": ["192.0.2.1", "192.0.2.3"]}}
    stale = client.patch(ZONE, json=second, headers={"If-Match": read})

    assert stale.status_code == 412
    assert "If-Match" in stale.json()["detail"]
    kept = _ok(client.get(ZONE)).json()["firewall"]["deny_sources"]
    assert kept == ["192.0.2.1/32", "192.0.2.2/32"]


def test_without_if_match_the_last_write_still_wins(client, zone):
    _ok(client.patch(ZONE, json={"firewall": {"deny_sources": ["192.0.2.2"]}}))
    _ok(client.patch(ZONE, json={"firewall": {"deny_sources": ["192.0.2.3"]}}))
    assert _ok(client.get(ZONE)).json()["firewall"]["deny_sources"] == ["192.0.2.3/32"]


@pytest.mark.parametrize(
    ("header", "status"),
    [
        ("*", 200),
        ('"stale", {current}', 200),
        ("W/{current}", 412),
        ("not-a-tag", 412),
    ],
    ids=["any", "list", "weak", "malformed"],
)
def test_if_match_forms(client, zone, header, status):
    current = zone.headers["ETag"]
    response = client.patch(
        ZONE,
        json={"cache_enabled": False},
        headers={"If-Match": header.format(current=current)},
    )
    assert response.status_code == status, response.text


def test_a_validation_error_is_not_masked_by_a_matching_tag(client, zone):
    response = client.patch(
        ZONE,
        json={"cache_enabled": None},
        headers={"If-Match": zone.headers["ETag"]},
    )
    assert response.status_code == 422


def test_records_rules_and_deletes_honour_if_match(client, zone):
    record = _ok(
        client.post(f"{ZONE}/records", json={"name": "www", "value": "192.0.2.10"}),
        201,
    ).headers["ETag"]
    assert _ok(client.get(RECORD)).headers["ETag"] == record
    _ok(client.patch(RECORD, json={"ttl": 600}, headers={"If-Match": record}))
    assert (
        client.patch(RECORD, json={"ttl": 900}, headers={"If-Match": record})
    ).status_code == 412
    assert client.delete(RECORD, headers={"If-Match": record}).status_code == 412
    _ok(client.get(RECORD))
    current = client.get(RECORD).headers["ETag"]
    _ok(client.delete(RECORD, headers={"If-Match": current}), 204)

    rule = _ok(
        client.post(
            f"{ZONE}/rules",
            json={
                "name": "api",
                "match": "api.example.com",
                "overrides": {"cache_enabled": False},
            },
        ),
        201,
    ).headers["ETag"]
    assert _ok(client.get(RULE)).headers["ETag"] == rule
    _ok(client.patch(RULE, json={"priority": 50}, headers={"If-Match": rule}))
    assert (
        client.patch(RULE, json={"priority": 60}, headers={"If-Match": rule})
    ).status_code == 412
    assert client.delete(RULE, headers={"If-Match": rule}).status_code == 412

    zone_tag = _ok(client.get(ZONE)).headers["ETag"]
    _ok(client.patch(ZONE, json={"cache_enabled": False}))
    assert client.delete(ZONE, headers={"If-Match": zone_tag}).status_code == 412
    _ok(client.get(ZONE))


def test_edges_honour_if_match(client):
    created = _ok(
        client.post("/v3/edges", json={"name": "edge-1", "host": "192.0.2.50"}),
        201,
    ).headers["ETag"]
    assert _ok(client.get(EDGE)).headers["ETag"] == created
    _ok(client.patch(EDGE, json={"port": 2222}, headers={"If-Match": created}))
    assert (
        client.patch(EDGE, json={"port": 2200}, headers={"If-Match": created})
    ).status_code == 412
    assert client.delete(EDGE, headers={"If-Match": created}).status_code == 412
    assert _ok(client.get(EDGE)).json()["port"] == 2222


def test_every_edit_that_takes_if_match_publishes_412_and_its_reads_publish_etag(
    settings,
):
    paths = control_plane_app(settings).openapi()["paths"]
    edits = {
        (path, method)
        for path, operations in paths.items()
        for method, operation in operations.items()
        if any(
            parameter["in"] == "header" and parameter["name"] == "If-Match"
            for parameter in operation.get("parameters", [])
        )
    }
    assert edits == {
        (ZONE.replace("example.com", "{domain}"), "patch"),
        (ZONE.replace("example.com", "{domain}"), "delete"),
        ("/v3/domains/{domain}/records/{type}/{name}", "patch"),
        ("/v3/domains/{domain}/records/{type}/{name}", "delete"),
        ("/v3/domains/{domain}/rules/{name}", "patch"),
        ("/v3/domains/{domain}/rules/{name}", "delete"),
        ("/v3/edges/{name}", "patch"),
        ("/v3/edges/{name}", "delete"),
    }
    for path, method in edits:
        assert "412" in paths[path][method]["responses"], (path, method)
        if method == "patch":
            success = paths[path][method]["responses"]["200"]
            assert "ETag" in success["headers"], (path, method)
            assert "ETag" in paths[path]["get"]["responses"]["200"]["headers"]


def test_if_match_parsing():
    assert IfMatch.parse(" * ").any_version
    assert IfMatch.parse('"a" , "b",').tags == frozenset({'"a"', '"b"'})
