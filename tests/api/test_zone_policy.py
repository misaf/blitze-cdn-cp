"""The zone's policy and its rules, over HTTP."""

from __future__ import annotations

from control_plane_fixtures import API_HEADERS, control_plane_app
from fastapi.testclient import TestClient

from blitzecdn.capabilities.dns.api.models import Domain as DomainModel
from blitzecdn.capabilities.dns.api.models import DomainPatch as DomainPatchModel
from blitzecdn.capabilities.dns.domain import Domain
from blitzecdn.capabilities.security.policy import SiteFirewall


def test_the_api_carries_every_zone_field_the_zone_has():
    """A knob an operator can set but never see would fail nowhere else.

    The published zone is written out rather than imported from `sites`, since
    a published surface may not cross a capability boundary. This is what makes
    the duplication safe: a field added to the zone is expected on both bodies.
    """
    published = set(DomainModel.model_fields)
    missing = set(Domain.model_fields) - published
    assert not missing, f"the API does not expose {sorted(missing)}"

    unpatchable = (
        set(Domain.model_fields) - {"name"} - set(DomainPatchModel.model_fields)
    )
    assert not unpatchable, f"the API cannot PATCH {sorted(unpatchable)}"


def test_a_zone_is_created_read_and_patched(settings):
    with TestClient(control_plane_app(settings)) as client:
        created = client.post(
            "/v1/domains",
            json={
                "name": "example.com",
                "cache_valid_success": "1h",
            },
            headers=API_HEADERS,
        )
        assert created.status_code == 201
        assert created.json()["cache_valid_success"] == "1h"

        patched = client.patch(
            "/v1/domains/example.com",
            json={"cache_valid_success": "5m"},
            headers=API_HEADERS,
        )
        assert patched.status_code == 200
        assert patched.json()["cache_valid_success"] == "5m"

        assert (
            client.get("/v1/domains/example.com", headers=API_HEADERS).json()[
                "cache_valid_success"
            ]
            == "5m"
        )


def test_a_zone_patch_that_breaks_a_cross_field_rule_is_refused(settings):
    with TestClient(control_plane_app(settings)) as client:
        client.post("/v1/domains", json={"name": "example.com"}, headers=API_HEADERS)
        response = client.patch(
            "/v1/domains/example.com",
            json={"http3_enabled": True},
            headers=API_HEADERS,
        )
        assert response.status_code == 422
        assert "http3_enabled" in response.text


def test_rules_are_written_listed_and_resolved(settings):
    with TestClient(control_plane_app(settings)) as client:
        client.post(
            "/v1/domains",
            json={"name": "example.com", "cache_valid_success": "10m"},
            headers=API_HEADERS,
        )
        assert (
            client.post(
                "/v1/domains/example.com/rules",
                json={
                    "name": "api",
                    "priority": 10,
                    "match": "api.example.com",
                    "overrides": {"cache_enabled": False},
                },
                headers=API_HEADERS,
            ).status_code
            == 201
        )
        client.post(
            "/v1/domains/example.com/rules",
            json={
                "name": "wide",
                "priority": 50,
                "match": "*",
                "overrides": {"cache_valid_success": "1h"},
            },
            headers=API_HEADERS,
        )

        listed = client.get("/v1/domains/example.com/rules", headers=API_HEADERS).json()
        assert [rule["name"] for rule in listed] == ["api", "wide"]

        resolved = client.get(
            "/v1/domains/example.com/resolve",
            params={"hostname": "api.example.com"},
            headers=API_HEADERS,
        ).json()
        assert resolved["rule"] == "api"
        assert resolved["policy"]["cache_enabled"] is False
        # `wide` matched as well and did not apply; first match wins whole.
        assert resolved["policy"]["cache_valid_success"] == "10m"

        fallback = client.get(
            "/v1/domains/example.com/resolve",
            params={"hostname": "www.example.com"},
            headers=API_HEADERS,
        ).json()
        assert fallback["rule"] == "wide"
        assert fallback["policy"]["cache_valid_success"] == "1h"


def test_an_override_the_zone_would_refuse_is_refused_on_the_rule(settings):
    with TestClient(control_plane_app(settings)) as client:
        client.post("/v1/domains", json={"name": "example.com"}, headers=API_HEADERS)
        response = client.post(
            "/v1/domains/example.com/rules",
            json={"name": "r", "overrides": {"cache_enabled": "maybe"}},
            headers=API_HEADERS,
        )
        assert response.status_code == 422


def test_a_rule_naming_a_hostname_outside_its_zone_is_refused(settings):
    with TestClient(control_plane_app(settings)) as client:
        client.post("/v1/domains", json={"name": "example.com"}, headers=API_HEADERS)
        response = client.post(
            "/v1/domains/example.com/rules",
            json={
                "name": "r",
                "match": "api.other.net",
                "overrides": {"cache_enabled": False},
            },
            headers=API_HEADERS,
        )
        assert response.status_code == 422
        assert "not inside" in response.text


def test_the_two_surfaces_replace_different_things(settings):
    """The CLI replaces one rule list; PATCH replaces the whole block.

    Both are reasonable readings of "set the firewall", and an operator who
    learns one on the command line and then scripts the other will be surprised
    by whichever they meet second — so the difference is asserted here rather
    than left to be discovered. `zone firewall` edits one rule at a time and
    keeps the lists it was not given; `PATCH` is a document update and the
    document it is given is the whole `firewall`.

    If the two are ever brought together, this is the test that should fail.
    """
    seeded = SiteFirewall(deny_sources=("203.0.113.0/24",), denied_countries=("DE",))

    # The command line: name one list, keep the other.
    merged = seeded.replacing({"deny_sources": ["198.51.100.0/24"]})
    assert merged.deny_sources == ("198.51.100.0/24",)
    assert merged.denied_countries == ("DE",)

    # The API: the same edit as a PATCH body drops what it does not mention.
    with TestClient(control_plane_app(settings)) as client:
        client.post("/v1/domains", json={"name": "example.com"}, headers=API_HEADERS)
        client.patch(
            "/v1/domains/example.com",
            json={"firewall": seeded.model_dump(mode="json")},
            headers=API_HEADERS,
        )
        response = client.patch(
            "/v1/domains/example.com",
            json={"firewall": {"deny_sources": ["198.51.100.0/24"]}},
            headers=API_HEADERS,
        )

    assert response.status_code == 200
    patched = response.json()["firewall"]
    assert patched["deny_sources"] == ["198.51.100.0/24"]
    assert patched.get("denied_countries", []) == [], (
        "PATCH replaces the whole block; a list it does not name is cleared"
    )
