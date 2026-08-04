import time

from fastapi.testclient import TestClient

from blitzecdn.api import create_app


def test_health_is_public_and_controls_require_auth(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/v1/sites").status_code == 401
        assert (
            client.get("/v1/sites", headers={"X-API-Key": "x" * 32}).status_code == 200
        )


def test_openapi_documents_control_and_certificate_workflows(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/docs").status_code == 200
        assert client.get("/redoc").status_code == 200
        schema = client.get("/openapi.json").json()
        paths = schema["paths"]
        assert "/v1/sites" in paths
        assert "/v1/deployments" in paths
        assert "/v1/sites/{name}/certificate/request" in paths
        assert "/v1/sites/{name}/certificate/upload" in paths


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


def test_deploy_returns_202_immediately_and_finishes_in_background(
    settings, site_payload
):
    """A convergence can outlast any HTTP client, so the request must not block."""
    headers = {"X-API-Key": "x" * 32}
    with TestClient(create_app(settings)) as client:
        assert (
            client.post("/v1/sites", json=site_payload, headers=headers).status_code
            == 201
        )
        queued = client.post("/v1/deployments", json={"check": True}, headers=headers)
        assert queued.status_code == 202
        deployment_id = queued.json()["id"]
        assert queued.json()["status"] == "queued"

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            body = client.get(
                f"/v1/deployments/{deployment_id}", headers=headers
            ).json()
            if body["status"] not in {"queued", "running"}:
                break
            time.sleep(0.02)
        # conftest points ansible_playbook at /usr/bin/true.
        assert body["status"] == "succeeded"
        assert body["return_code"] == 0


def test_repeated_bad_keys_are_throttled(settings):
    with TestClient(create_app(settings)) as client:
        codes = [
            client.get("/v1/sites", headers={"X-API-Key": "n" * 32}).status_code
            for _ in range(12)
        ]
    assert codes[:10] == [401] * 10
    assert codes[10:] == [429, 429]


def test_throttle_forgets_a_client_after_a_success(settings):
    good = {"X-API-Key": "x" * 32}
    bad = {"X-API-Key": "n" * 32}
    with TestClient(create_app(settings)) as client:
        for _ in range(9):
            assert client.get("/v1/sites", headers=bad).status_code == 401
        assert client.get("/v1/sites", headers=good).status_code == 200
        # The budget reset, so the next nine failures still are not throttled.
        for _ in range(9):
            assert client.get("/v1/sites", headers=bad).status_code == 401


def test_certificate_upload_and_metadata_api(settings, site_payload, certificate_pair):
    headers = {"X-API-Key": "x" * 32}
    certificate, key = certificate_pair()
    with TestClient(create_app(settings)) as client:
        assert (
            client.post("/v1/sites", json=site_payload, headers=headers).status_code
            == 201
        )
        uploaded = client.post(
            "/v1/sites/example-cdn/certificate/upload",
            files={
                "certificate": ("fullchain.pem", certificate, "application/x-pem-file"),
                "private_key": ("privkey.pem", key, "application/x-pem-file"),
            },
            headers=headers,
        )
        assert uploaded.status_code == 200
        body = uploaded.json()
        assert body["source"] == "uploaded"
        assert "private_key" not in body
        metadata = client.get("/v1/sites/example-cdn/certificate", headers=headers)
        assert metadata.json() == body
