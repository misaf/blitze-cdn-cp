from fastapi.testclient import TestClient

from blitzecdn.api import create_app


def test_health_is_public_and_controls_require_auth(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/v1/sites").status_code == 401
        assert (
            client.get("/v1/sites", headers={"X-API-Key": "x" * 32}).status_code == 200
        )


def test_site_crud_and_errors(settings, site_payload):
    headers = {"X-API-Key": "x" * 32}
    with TestClient(create_app(settings)) as client:
        created = client.post("/v1/sites", json=site_payload, headers=headers)
        assert created.status_code == 201
        assert (
            client.post("/v1/sites", json=site_payload, headers=headers).status_code
            == 409
        )
        patched = client.patch(
            "/v1/sites/example-cdn", json={"cache_enabled": False}, headers=headers
        )
        assert patched.json()["cache_enabled"] is False
        assert (
            client.delete("/v1/sites/example-cdn", headers=headers).status_code == 204
        )
        assert client.get("/v1/deployments/missing", headers=headers).status_code == 404


def test_api_fails_closed_without_keys(settings):
    insecure = settings.model_copy(update={"api_keys": {}})
    with TestClient(create_app(insecure)) as client:
        assert client.get("/v1/sites").status_code == 503
