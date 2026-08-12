import threading
import time

from conftest import FakeRunner, ansible_run, host_run
from fastapi.testclient import TestClient

from blitzecdn import __version__
from blitzecdn.api import create_app
from blitzecdn.application import (
    CertificateService,
    DeploymentService,
    EdgeOperationsService,
)
from blitzecdn.exceptions import (
    ConfigurationError,
    DeploymentBusyError,
    ExecutionError,
)


def test_health_is_public_and_controls_require_auth(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/v1/sites").status_code == 401
        assert (
            client.get("/v1/sites", headers={"X-API-Key": "x" * 32}).status_code == 200
        )


def test_api_service_runs_certificate_reconciliation_on_its_interval(
    settings, monkeypatch
):
    called = threading.Event()
    configured = settings.model_copy(
        update={"certificate_reconcile_interval_seconds": 1}
    )

    def reconcile(_self, operator):
        assert operator == "scheduler"
        called.set()
        return {"issued": [], "skipped": {}, "failed": {}, "deployment": None}

    monkeypatch.setattr(CertificateService, "reconcile_certificates", reconcile)

    with TestClient(create_app(configured)):
        assert called.wait(2)


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
        # conftest points ansible_playbook at /usr/bin/true, so a real process
        # runs and exits 0 without ever loading the callback — which is exactly
        # the shape of a run that produced no per-host result. The deployment
        # still carries a result, and it still names the log.
        assert body["status"] == "succeeded"
        assert body["result"]["return_code"] == 0
        assert body["result"]["hosts"] == []
        assert body["result"]["log_path"].endswith(".log")


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


def test_a_single_site_is_readable_by_name(settings, domain_payload, record_payload):
    with TestClient(create_app(settings)) as client:
        client.post("/v1/domains", json=domain_payload, headers=_HEADERS)
        client.post(
            "/v1/domains/example.com/records",
            json={**record_payload, "proxied": True},
            headers=_HEADERS,
        )
        name = client.get("/v1/sites", headers=_HEADERS).json()[0]["name"]

        response = client.get(f"/v1/sites/{name}", headers=_HEADERS)

        assert response.status_code == 200
        assert response.json()["name"] == name
        # The derived defaults, which the record never carried.
        assert response.json()["origin_scheme"] == "https"


def test_an_unknown_site_is_a_404(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/v1/sites/absent", headers=_HEADERS).status_code == 404


def test_renewal_can_be_narrowed_to_named_sites(settings):
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/certificates/renew",
            json={"sites": ["absent-example-com"]},
            headers=_HEADERS,
        )
        # No certificate by that name, so the selector is a 404 rather than an
        # empty success an automated caller would read as "nothing was due".
        assert response.status_code == 404


def test_an_empty_renewal_selector_is_a_422(settings):
    """`[]` would otherwise mean 'renew nothing' while reading as a filter."""
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/certificates/renew", json={"sites": []}, headers=_HEADERS
        )
        assert response.status_code == 422


def _proxied_site(client, domain_payload, record_payload):
    client.post("/v1/domains", json=domain_payload, headers=_HEADERS)
    client.post(
        "/v1/domains/example.com/records",
        json={**record_payload, "proxied": True},
        headers=_HEADERS,
    )
    return client.get("/v1/sites", headers=_HEADERS).json()[0]["server_names"][0]


def test_purging_a_hostname_no_site_serves_is_a_404(settings):
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/cache/purge",
            json={"entries": [{"host": "nothing.example.com", "uri": "/x"}]},
            headers=_HEADERS,
        )
        assert response.status_code == 404


def test_an_empty_purge_request_is_a_409(settings):
    """Neither entries nor purge_all: refused rather than reported as done."""
    with TestClient(create_app(settings)) as client:
        response = client.post("/v1/cache/purge", json={}, headers=_HEADERS)
        assert response.status_code == 409


def test_purging_everything_and_entries_at_once_is_a_409(settings):
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/cache/purge",
            json={
                "purge_all": True,
                "entries": [{"host": "cdn.example.com", "uri": "/x"}],
            },
            headers=_HEADERS,
        )
        assert response.status_code == 409


def test_a_purge_uri_that_is_not_a_path_is_a_422(settings):
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/cache/purge",
            json={"entries": [{"host": "cdn.example.com", "uri": "app.js"}]},
            headers=_HEADERS,
        )
        assert response.status_code == 422


def test_a_purge_host_limit_that_could_widen_is_a_422(settings):
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/cache/purge",
            json={"purge_all": True, "host_limit": "edge-a:!edge-b"},
            headers=_HEADERS,
        )
        assert response.status_code == 422


def test_a_partial_purge_is_a_409_that_still_carries_the_whole_result(
    settings, seeded, monkeypatch
):
    """The dangerous outcome has to stay machine-readable.

    A partial purge is exactly when a caller needs the detail — which edges
    dropped the object and which are still serving it — so the 409 carries the
    same PurgeResult a success would, rather than replacing it with prose to be
    parsed under time pressure.
    """
    control, _ = seeded(
        FakeRunner(
            [ansible_run(host_run("edge-a"), host_run("edge-b", failure="rm failed"))]
        )
    )
    monkeypatch.setattr("blitzecdn.api.build_control_plane", lambda _settings: control)

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/cache/purge",
            # The derived site has no certificate, so it serves http and only
            # an http entry could have been cached.
            json={
                "entries": [
                    {"host": "cdn.example.com", "uri": "/app.js", "scheme": "http"}
                ]
            },
            headers=_HEADERS,
        )

    assert response.status_code == 409
    body = response.json()
    assert body["complete"] is False
    assert body["failed_hosts"] == ["edge-b"]
    assert {host["host"] for host in body["hosts"]} == {"edge-a", "edge-b"}


def test_a_complete_purge_is_a_200_saying_so(settings, seeded, monkeypatch):
    control, _ = seeded(FakeRunner([ansible_run(host_run("edge-a"))]))
    monkeypatch.setattr("blitzecdn.api.build_control_plane", lambda _settings: control)

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/cache/purge",
            json={
                "entries": [
                    {"host": "cdn.example.com", "uri": "/app.js", "scheme": "http"}
                ]
            },
            headers=_HEADERS,
        )

    assert response.status_code == 200
    assert response.json()["complete"] is True
    assert response.json()["failed_hosts"] == []


# ----------------------------------------------------------------------
# Error mapping. A permanent condition must not be reported as a transient
# one: a client that retries a ConfigurationError retries forever, and an
# alerting rule watching for 503 pages someone about a controller that is
# working correctly and declining bad input.
# ----------------------------------------------------------------------


def test_a_configuration_error_is_a_400_rather_than_a_503(settings, monkeypatch):
    monkeypatch.setattr(
        EdgeOperationsService,
        "check_origins",
        lambda _self: (_ for _ in ()).throw(ConfigurationError("no edges configured")),
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/v1/origins/check", headers=_HEADERS)
    assert response.status_code == 400
    assert response.json()["detail"] == "no edges configured"


def test_an_execution_error_is_a_502(settings, monkeypatch):
    monkeypatch.setattr(
        EdgeOperationsService,
        "check_origins",
        lambda _self: (_ for _ in ()).throw(ExecutionError("ansible would not start")),
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/v1/origins/check", headers=_HEADERS)
    assert response.status_code == 502


def test_a_busy_deployment_is_a_409_that_says_when_to_come_back(settings, monkeypatch):
    """The one conflict that clears on its own, so the one that gets Retry-After."""

    def busy(_self, _operator, **_kwargs):
        raise DeploymentBusyError("another deployment is already running")

    monkeypatch.setattr(DeploymentService, "submit_deployment", busy)
    with TestClient(create_app(settings)) as client:
        response = client.post("/v1/deployments", json={}, headers=_HEADERS)
    assert response.status_code == 409
    assert response.headers["Retry-After"] == "30"


def test_renewal_is_bounded_by_the_configured_budget(settings, monkeypatch):
    """A sweep served over HTTP must not run until the CA gets bored."""
    seen: dict[str, object] = {}

    def renew(_self, operator, **kwargs):
        seen.update(kwargs)
        seen["operator"] = operator
        return {"renewed": [], "skipped": [], "failed": []}

    configured = settings.model_copy(update={"certificate_renewal_budget_seconds": 42})
    monkeypatch.setattr(CertificateService, "renew_certificates", renew)
    with TestClient(create_app(configured)) as client:
        response = client.post("/v1/certificates/renew", json={}, headers=_HEADERS)

    assert response.status_code == 200
    assert seen["budget_seconds"] == 42


def test_renewal_does_not_occupy_the_shared_request_thread_pool(settings, monkeypatch):
    """It runs on the renewal pool, so /health keeps answering during a sweep.

    The whole failure this guards against is a controller that looks dead to a
    load balancer while it is working perfectly, so the assertion is the thread
    name rather than a timing measurement.
    """
    names: list[str] = []

    def renew(_self, _operator, **_kwargs):
        names.append(threading.current_thread().name)
        return {"renewed": [], "skipped": [], "failed": []}

    monkeypatch.setattr(CertificateService, "renew_certificates", renew)
    with TestClient(create_app(settings)) as client:
        assert (
            client.post("/v1/certificates/renew", json={}, headers=_HEADERS).status_code
            == 200
        )

    assert names and names[0].startswith("blitzecdn-renewal")


def _seed_proxied_record(client) -> None:
    client.post("/v1/domains", json={"name": "example.com"}, headers=_HEADERS)
    client.post(
        "/v1/domains/example.com/records",
        json={
            "domain": "example.com",
            "name": "cdn",
            "value": "198.51.100.10",
            "proxied": True,
        },
        headers=_HEADERS,
    )


def _stub_preflight(monkeypatch, *failures: str) -> None:
    """Replace the real checks, which would resolve names and probe an origin."""
    from blitzecdn.domain.certificates import (
        PreflightCheck,
        PreflightReport,
        PreflightSeverity,
    )
    from blitzecdn.infrastructure.preflight import CertificatePreflight

    def check(self, site, *, deployed, record_ttl=None):
        return PreflightReport(
            site=site.name,
            checks=tuple(
                PreflightCheck(
                    name=name,
                    passed=False,
                    severity=PreflightSeverity.BLOCKING,
                    detail=f"{name} failed",
                )
                for name in failures
            ),
        )

    monkeypatch.setattr(CertificatePreflight, "check", check)


def test_the_preflight_endpoint_reports_readiness(settings, monkeypatch):
    _stub_preflight(monkeypatch)
    with TestClient(create_app(settings)) as client:
        _seed_proxied_record(client)

        response = client.get(
            "/v1/sites/cdn-example-com/certificate/preflight", headers=_HEADERS
        )

        assert response.status_code == 200
        assert response.json() == {"site": "cdn-example-com", "checks": []}


def test_the_preflight_endpoint_requires_auth(settings, monkeypatch):
    _stub_preflight(monkeypatch)
    with TestClient(create_app(settings)) as client:
        assert (
            client.get("/v1/sites/cdn-example-com/certificate/preflight").status_code
            == 401
        )


def test_a_blocked_preflight_makes_a_request_a_409(settings, monkeypatch):
    _stub_preflight(monkeypatch, "dns")
    with TestClient(create_app(settings)) as client:
        _seed_proxied_record(client)

        response = client.post(
            "/v1/sites/cdn-example-com/certificate/request",
            json={"email": "ops@example.com"},
            headers=_HEADERS,
        )

        assert response.status_code == 409
        assert "preflight failed" in response.json()["detail"]


def test_skip_preflight_is_rejected_as_a_non_boolean(settings, monkeypatch):
    """`extra="forbid"` plus a typed field: a typo cannot silently disable this."""
    _stub_preflight(monkeypatch, "dns")
    with TestClient(create_app(settings)) as client:
        _seed_proxied_record(client)

        assert (
            client.post(
                "/v1/sites/cdn-example-com/certificate/request",
                json={"email": "ops@example.com", "skip_preflght": True},
                headers=_HEADERS,
            ).status_code
            == 422
        )
