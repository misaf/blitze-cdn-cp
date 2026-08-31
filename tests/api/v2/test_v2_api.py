from fastapi.testclient import TestClient

from blitzecdn.api import create_app
from blitzecdn.api.v2_models import RecordPatchV2 as V2RecordPatch
from blitzecdn.api.v2_models import SitePolicyV2
from blitzecdn.features.sites.domain import SitePolicy


def test_v2_carries_every_policy_field_the_domain_has():
    """The live version is the one that must not silently omit a setting.

    v1 is projected on the way out, so a missing field there is deliberate. v2
    is not, and a knob an operator can set but never see would fail nowhere
    else.
    """
    missing = set(SitePolicy.model_fields) - set(SitePolicyV2.model_fields)
    assert not missing, f"v2 does not expose {sorted(missing)}"

    unpatchable = set(SitePolicy.model_fields) - set(V2RecordPatch.model_fields)
    assert not unpatchable, f"v2 cannot PATCH {sorted(unpatchable)}"


def test_no_cloudflare_header_name_is_published_by_the_api(settings):
    """The BZ- namespace is the whole surface; CF- and True-Client-IP are not
    ours to define and must not appear as fields, defaults, or descriptions."""
    with TestClient(create_app(settings)) as client:
        document = client.get("/openapi.json").text

    for foreign in ("CF-Connecting-IP", "cf_connecting_ip", "True-Client-IP"):
        assert foreign not in document


def test_http3_create_read_patch_and_validation(settings):
    headers = {"X-API-Key": "x" * 32}
    record = {
        "domain": "example.com",
        "name": "cdn",
        "value": "198.51.100.10",
        "proxied": True,
        "ssl_mode": "flexible",
        "http3_enabled": True,
        "certificate_mode": "existing",
        "certificate_path": "/etc/ssl/certs/edge.pem",
        "certificate_key_path": "/etc/ssl/private/edge.key",
    }
    with TestClient(create_app(settings)) as client:
        created_domain = client.post(
            "/v2/domains", json={"name": "example.com"}, headers=headers
        )
        assert created_domain.status_code == 201
        created = client.post(
            "/v2/domains/example.com/records", json=record, headers=headers
        )
        assert created.status_code == 201
        assert created.json()["http3_enabled"] is True
        assert (
            client.get("/v2/sites", headers=headers).json()[0]["http3_enabled"] is True
        )

        unchanged = client.patch(
            "/v2/domains/example.com/records/cdn",
            json={"http3_enabled": True},
            headers=headers,
        )
        assert unchanged.status_code == 200
        assert unchanged.json()["http3_enabled"] is True

        disabled = client.patch(
            "/v2/domains/example.com/records/cdn",
            json={"http3_enabled": False},
            headers=headers,
        )
        assert disabled.status_code == 200
        assert disabled.json()["http3_enabled"] is False

        rejected = client.patch(
            "/v2/domains/example.com/records/cdn",
            json={"ssl_mode": "off", "http3_enabled": True},
            headers=headers,
        )
        assert rejected.status_code == 422
        assert "requires ssl_mode" in rejected.text

        events = client.get("/v2/audit-events", headers=headers).json()
        updates = [event for event in events if event["action"] == "record.updated"]
        assert any("http3_enabled" in event["details"]["fields"] for event in updates)


def test_under_attack_mode_is_visible_patchable_and_in_openapi(settings):
    headers = {"X-API-Key": "x" * 32}
    with TestClient(create_app(settings)) as client:
        schema = client.get("/openapi.json").json()
        property_schema = schema["components"]["schemas"]["RecordPatchV2"][
            "properties"
        ]["under_attack_mode"]
        assert property_schema["anyOf"][0]["type"] == "boolean"

        client.post("/v2/domains", json={"name": "example.com"}, headers=headers)
        created = client.post(
            "/v2/domains/example.com/records",
            json={
                "domain": "example.com",
                "name": "cdn",
                "value": "198.51.100.10",
                "proxied": True,
            },
            headers=headers,
        )
        assert created.status_code == 201
        assert created.json()["under_attack_mode"] is False

        patched = client.patch(
            "/v2/domains/example.com/records/cdn",
            json={"under_attack_mode": True},
            headers=headers,
        )
        assert patched.status_code == 200
        assert patched.json()["under_attack_mode"] is True
        assert (
            client.get("/v2/sites", headers=headers).json()[0]["under_attack_mode"]
            is True
        )

        invalid = client.patch(
            "/v2/domains/example.com/records/cdn",
            json={"under_attack_mode": "sometimes"},
            headers=headers,
        )
        assert invalid.status_code == 422
