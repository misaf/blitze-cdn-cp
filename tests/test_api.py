import time

from fastapi.testclient import TestClient

from blitzecdn import __version__
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
        assert schema["info"]["version"] == __version__
        paths = schema["paths"]
        assert "/v1/sites" in paths
        assert "/v1/deployments" in paths
        assert "/v1/sites/{name}/certificate/request" in paths
        assert "/v1/sites/{name}/certificate/upload" in paths


def test_domain_and_record_crud_and_errors(settings, domain_payload, record_payload):
    headers = {"X-API-Key": "x" * 32}
    with TestClient(create_app(settings)) as client:
        assert (
            client.post("/v1/domains", json=domain_payload, headers=headers).status_code
            == 201
        )
        assert (
            client.post("/v1/domains", json=domain_payload, headers=headers).status_code
            == 409
        )
        # A record cannot exist without its zone.
        orphan = client.post(
            "/v1/domains/absent.example/records",
            json={**record_payload, "domain": "absent.example"},
            headers=headers,
        )
        assert orphan.status_code == 404

        created = client.post(
            "/v1/domains/example.com/records", json=record_payload, headers=headers
        )
        assert created.status_code == 201
        assert created.json()["proxied"] is True

        # The body's domain must agree with the path.
        mismatched = client.post(
            "/v1/domains/example.com/records",
            json={**record_payload, "domain": "other.example", "name": "x"},
            headers=headers,
        )
        assert mismatched.status_code == 409

        # Proxying a record is what creates the edge virtual host.
        assert len(client.get("/v1/sites", headers=headers).json()) == 1

        toggled = client.patch(
            "/v1/domains/example.com/records/cdn",
            json={"proxied": False},
            headers=headers,
        )
        assert toggled.json()["proxied"] is False
        assert client.get("/v1/sites", headers=headers).json() == []

        assert (
            client.delete(
                "/v1/domains/example.com/records/cdn", headers=headers
            ).status_code
            == 204
        )
        assert (
            client.delete("/v1/domains/example.com", headers=headers).status_code == 204
        )
        assert client.get("/v1/deployments/missing", headers=headers).status_code == 404


def test_sites_are_read_only(settings, site_payload):
    """Sites are derived, so the mutation routes must not exist."""
    headers = {"X-API-Key": "x" * 32}
    with TestClient(create_app(settings)) as client:
        assert (
            client.post("/v1/sites", json=site_payload, headers=headers).status_code
            == 405
        )
        schema = client.get("/openapi.json").json()
        assert set(schema["paths"]["/v1/sites"]) == {"get"}


def test_dns_export_omits_addresses_for_proxied_records(
    settings, domain_payload, record_payload
):
    """A proxied name must resolve to an edge, and edge IPs are not ours."""
    headers = {"X-API-Key": "x" * 32}
    with TestClient(create_app(settings)) as client:
        client.post("/v1/domains", json=domain_payload, headers=headers)
        client.post(
            "/v1/domains/example.com/records", json=record_payload, headers=headers
        )
        client.post(
            "/v1/domains/example.com/records",
            json={**record_payload, "name": "db", "proxied": False},
            headers=headers,
        )
        exported = {
            row["fqdn"]: row
            for row in client.get("/v1/dns/export", headers=headers).json()
        }
        assert "value" not in exported["cdn.example.com"]
        assert exported["cdn.example.com"]["origin"] == "198.51.100.10"
        assert exported["db.example.com"]["value"] == "198.51.100.10"


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
        client.post("/v1/domains", json={"name": "example.com"}, headers=headers)
        assert (
            client.post(
                "/v1/domains/example.com/records",
                json={
                    "domain": "example.com",
                    "name": "cdn",
                    "value": "198.51.100.10",
                    "proxied": True,
                },
                headers=headers,
            ).status_code
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
        client.post("/v1/domains", json={"name": "example.com"}, headers=headers)
        assert (
            client.post(
                "/v1/domains/example.com/records",
                json={
                    "domain": "example.com",
                    "name": "cdn",
                    "value": "198.51.100.10",
                    "proxied": True,
                },
                headers=headers,
            ).status_code
            == 201
        )
        uploaded = client.post(
            "/v1/sites/cdn-example-com/certificate/upload",
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
        metadata = client.get("/v1/sites/cdn-example-com/certificate", headers=headers)
        assert metadata.json() == body


_HEADERS = {"X-API-Key": "x" * 32}


def test_a_deploy_can_be_narrowed_to_some_edges(settings):
    with TestClient(create_app(settings)) as client:
        accepted = client.post(
            "/v1/deployments",
            json={"check": True, "host_limit": "edge-a"},
            headers=_HEADERS,
        )
        assert accepted.status_code == 202
        assert accepted.json()["host_limit"] == "edge-a"


def test_a_limit_that_could_widen_a_deploy_is_a_422(settings):
    """Rejected at the schema, so no deployment row is created to explain."""
    with TestClient(create_app(settings)) as client:
        for pattern in ("edge-a:database-1", "edge-a:!edge-b", "@/etc/passwd"):
            response = client.post(
                "/v1/deployments",
                json={"host_limit": pattern},
                headers=_HEADERS,
            )
            assert response.status_code == 422, pattern
        assert client.get("/v1/deployments", headers=_HEADERS).json() == []


def test_drift_queues_a_check_run_and_reads_back_as_a_report(settings):
    with TestClient(create_app(settings)) as client:
        queued = client.post("/v1/drift", json={}, headers=_HEADERS)
        assert queued.status_code == 202
        assert queued.json()["check_mode"] is True

        deployment_id = queued.json()["id"]
        for _ in range(50):
            report = client.get(
                f"/v1/deployments/{deployment_id}/drift", headers=_HEADERS
            )
            if report.status_code == 200:
                break
            time.sleep(0.05)
        assert report.status_code == 200
        assert report.json()["deployment_id"] == deployment_id


def test_an_applied_deployment_is_not_readable_as_drift(settings):
    with TestClient(create_app(settings)) as client:
        queued = client.post("/v1/deployments", json={"check": False}, headers=_HEADERS)
        deployment_id = queued.json()["id"]
        response = client.get(
            f"/v1/deployments/{deployment_id}/drift", headers=_HEADERS
        )
        assert response.status_code == 409


def test_certificates_and_origins_are_readable(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/v1/certificates", headers=_HEADERS).json() == []
        assert (
            client.get("/v1/certificates?expiring_in=30", headers=_HEADERS).json() == []
        )
        assert client.get("/v1/origins/check", headers=_HEADERS).json() == []
        assert (
            client.post("/v1/certificates/renew", json={}, headers=_HEADERS).json()[
                "renewed"
            ]
            == []
        )
