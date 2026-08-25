import threading
import time

from conftest import FakeRunner, ansible_run, host_run
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from blitzecdn import __version__
from blitzecdn.api import create_app
from blitzecdn.api.dependencies import get_control_plane
from blitzecdn.api.routes import (
    cache,
    certificates,
    deployments,
    diagnostics,
    edges,
    sites,
    zones,
)
from blitzecdn.application import (
    CertificateService,
    DeploymentService,
    EdgeOperationsService,
)
from blitzecdn.domain.operations import WorkflowKind
from blitzecdn.exceptions import (
    ConfigurationError,
    DeploymentBusyError,
    ExecutionError,
)
from blitzecdn.infrastructure.database import Repository
from blitzecdn.infrastructure.operation_stores import WorkflowStore


def test_create_app_defers_control_plane_io_until_lifespan(settings, monkeypatch):
    built = []

    def build(*_args, **_kwargs):
        built.append(True)
        raise AssertionError("construction must happen in lifespan")

    monkeypatch.setattr("blitzecdn.api.build_control_plane", build)
    create_app(settings)
    assert built == []


def test_routes_are_domain_modules_and_control_plane_is_a_dependency():
    routers = (diagnostics, sites, zones, edges, cache, certificates, deployments)
    routes = [
        route
        for module in routers
        for route in module.router.routes
        if isinstance(route, APIRoute)
    ]
    modules = {
        route.endpoint.__module__
        for route in routes
        if route.path in {"/health", "/metrics"} or route.path.startswith("/v1/")
    }
    assert modules == {
        "blitzecdn.api.routes.cache",
        "blitzecdn.api.routes.certificates",
        "blitzecdn.api.routes.deployments",
        "blitzecdn.api.routes.diagnostics",
        "blitzecdn.api.routes.edges",
        "blitzecdn.api.routes.sites",
        "blitzecdn.api.routes.zones",
    }

    def dependency_calls(route: APIRoute) -> set[object]:
        pending = list(route.dependant.dependencies)
        calls: set[object] = set()
        while pending:
            dependency = pending.pop()
            calls.add(dependency.call)
            pending.extend(dependency.dependencies)
        return calls

    for route in routes:
        if route.path in {"/health", "/metrics"} or route.path.startswith("/v1/"):
            assert get_control_plane in dependency_calls(route), route.path


def test_health_is_public_and_controls_require_auth(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/metrics").status_code == 401
        assert client.get("/v1/sites").status_code == 401
        wrong = client.get("/v1/sites", headers={"X-API-Key": "wrong"})
        assert wrong.status_code == 401
        assert wrong.headers["WWW-Authenticate"] == "ApiKey"
        assert (
            client.get("/v1/sites", headers={"X-API-Key": "x" * 32}).status_code == 200
        )


def test_health_reports_redis_unavailable(settings, monkeypatch):
    monkeypatch.setattr("blitzecdn.control_plane.redis_ready", lambda _url: False)
    with TestClient(create_app(settings)) as client:
        response = client.get("/health")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "detail": "ConnectionError"}


def test_api_service_runs_certificate_reconciliation_on_its_interval(
    settings, monkeypatch
):
    called = threading.Event()
    configured = settings.model_copy(
        update={"certificate_reconcile_interval_seconds": 1}
    )

    def enqueue(_url, operation, *, ttl_seconds):
        assert operation == "reconcile-certificates"
        assert ttl_seconds >= 2
        called.set()
        return True

    monkeypatch.setattr("blitzecdn.scheduler.enqueue_scheduled_once", enqueue)

    with TestClient(create_app(configured)):
        assert called.wait(2)


def test_api_service_runs_automatic_ssl_scans_on_their_interval(settings, monkeypatch):
    called = threading.Event()
    configured = settings.model_copy(
        update={
            "certificate_reconcile_interval_seconds": 0,
            "certificate_renewal_interval_seconds": 0,
            "drift_check_interval_seconds": 0,
            "ssl_automatic_scan_interval_seconds": 1,
        }
    )

    def enqueue(_url, operation, *, ttl_seconds):
        assert operation == "reconcile-automatic-ssl"
        assert ttl_seconds >= 2
        called.set()
        return True

    monkeypatch.setattr("blitzecdn.scheduler.enqueue_scheduled_once", enqueue)

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
        assert "/v1/workflows" in paths


def test_openapi_declares_the_api_key_as_a_security_scheme(settings):
    """The key is authentication, not a header to be typed into every request.

    Clients that generate from the schema (Swagger UI, Postman) only offer a
    collection-level credential when the operations reference a security
    scheme, so a regression back to `Header(...)` has to fail here.
    """
    with TestClient(create_app(settings)) as client:
        schema = client.get("/openapi.json").json()

    schemes = schema["components"]["securitySchemes"]
    assert schemes["ApiKeyAuth"]["type"] == "apiKey"
    assert schemes["ApiKeyAuth"]["in"] == "header"
    assert schemes["ApiKeyAuth"]["name"] == "x-api-key"

    # No deployment-specific base URL, and no key value, belongs in the schema.
    assert "servers" not in schema
    assert "x" * 32 not in client.get("/openapi.json").text

    public = {("/health", "get")}
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            parameters = {
                parameter["name"].lower()
                for parameter in operation.get("parameters", ())
            }
            assert "x-api-key" not in parameters, f"{method} {path}"
            if (path, method) in public:
                assert "security" not in operation
                continue
            assert operation["security"] == [{"ApiKeyAuth": []}], f"{method} {path}"


def test_interrupted_workflows_are_recovered_and_visible(settings):
    repository = Repository(settings.database_path)
    workflow = repository.workflows.create(
        "interrupted", WorkflowKind.CERTIFICATE, "alice", "cdn-example-com"
    )

    with TestClient(create_app(settings)) as client:
        assert client.get("/v1/workflows").status_code == 401
        response = client.get("/v1/workflows", headers={"X-API-Key": "x" * 32})
        detail = client.get(
            f"/v1/workflows/{workflow.id}", headers={"X-API-Key": "x" * 32}
        )

    assert response.status_code == 200
    assert response.json()[0]["status"] == "needs_review"
    assert detail.json()["error"].startswith("the controller restarted")


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
        sites = client.get("/v1/sites", headers=headers).json()
        assert len(sites) == 1
        assert sites[0]["always_use_https"] is False
        assert sites[0]["minimum_tls_version"] == "1.2"
        assert sites[0]["cache_query_string_mode"] == "include"

        redirect = client.patch(
            "/v1/domains/example.com/records/cdn",
            json={"always_use_https": True},
            headers=headers,
        )
        assert redirect.status_code == 200
        assert redirect.json()["always_use_https"] is True
        assert (
            client.get("/v1/sites", headers=headers).json()[0]["always_use_https"]
            is True
        )

        policy = client.patch(
            "/v1/domains/example.com/records/cdn",
            json={
                "minimum_tls_version": "1.3",
                "cache_query_string_mode": "ignore",
            },
            headers=headers,
        )
        assert policy.status_code == 200
        assert policy.json()["minimum_tls_version"] == "1.3"
        assert policy.json()["cache_query_string_mode"] == "ignore"

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


def test_deploy_returns_202_immediately_and_stays_durable_until_a_worker_runs(
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

        body = client.get(f"/v1/deployments/{deployment_id}", headers=headers).json()
        assert body["status"] == "queued"


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


def test_certificates_are_readable(settings):
    # Origins are no longer among these: the check runs a playbook across the
    # fleet now, so it is a POST like the cache-statistics route rather than a
    # read the controller can answer by itself.
    with TestClient(create_app(settings)) as client:
        assert client.get("/v1/certificates", headers=_HEADERS).json() == []
        assert (
            client.get("/v1/certificates?expiring_in=30", headers=_HEADERS).json() == []
        )
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
        assert response.json()["ssl_mode"] == "off"
        assert response.json()["ssl_automatic_mode"] == "auto"
        assert "origin_scheme" not in response.json()


def test_automatic_ssl_reconciliation_is_exposed_by_the_api(settings):
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/ssl/automatic/reconcile",
            headers=_HEADERS,
        )

    assert response.status_code == 200
    assert response.json() == {
        "scanned": [],
        "upgraded": {},
        "skipped": {},
        "deployment": None,
    }


def test_removed_origin_scheme_is_rejected_on_create(
    settings, domain_payload, record_payload
):
    with TestClient(create_app(settings)) as client:
        client.post("/v1/domains", json=domain_payload, headers=_HEADERS)
        response = client.post(
            "/v1/domains/example.com/records",
            json={**record_payload, "origin_scheme": "https"},
            headers=_HEADERS,
        )
        assert response.status_code == 422


def test_removed_origin_scheme_is_rejected_on_patch(
    settings, domain_payload, record_payload
):
    with TestClient(create_app(settings)) as client:
        client.post("/v1/domains", json=domain_payload, headers=_HEADERS)
        client.post(
            "/v1/domains/example.com/records",
            json=record_payload,
            headers=_HEADERS,
        )
        response = client.patch(
            "/v1/domains/example.com/records/cdn",
            params={"type": "A"},
            json={"origin_scheme": "http"},
            headers=_HEADERS,
        )
        assert response.status_code == 422


def test_removed_origin_port_is_rejected_on_create(
    settings, domain_payload, record_payload
):
    with TestClient(create_app(settings)) as client:
        client.post("/v1/domains", json=domain_payload, headers=_HEADERS)
        response = client.post(
            "/v1/domains/example.com/records",
            json={**record_payload, "origin_port": 8080},
            headers=_HEADERS,
        )
        assert response.status_code == 422


def test_removed_origin_port_is_rejected_on_patch(
    settings, domain_payload, record_payload
):
    with TestClient(create_app(settings)) as client:
        client.post("/v1/domains", json=domain_payload, headers=_HEADERS)
        client.post(
            "/v1/domains/example.com/records",
            json=record_payload,
            headers=_HEADERS,
        )
        response = client.patch(
            "/v1/domains/example.com/records/cdn",
            params={"type": "A"},
            json={"origin_port": 8080},
            headers=_HEADERS,
        )
        assert response.status_code == 422


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
    monkeypatch.setattr(
        "blitzecdn.api.build_control_plane", lambda _settings, **_kwargs: control
    )

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
    monkeypatch.setattr(
        "blitzecdn.api.build_control_plane", lambda _settings, **_kwargs: control
    )

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
        lambda _self, _operator, **_kwargs: (_ for _ in ()).throw(
            ConfigurationError("no edges configured")
        ),
    )
    with TestClient(create_app(settings)) as client:
        response = client.post("/v1/origins/check", json={}, headers=_HEADERS)
    assert response.status_code == 400
    assert response.json()["detail"] == "no edges configured"


def test_an_execution_error_is_a_502(settings, monkeypatch):
    monkeypatch.setattr(
        EdgeOperationsService,
        "check_origins",
        lambda _self, _operator, **_kwargs: (_ for _ in ()).throw(
            ExecutionError("ansible would not start")
        ),
    )
    with TestClient(create_app(settings)) as client:
        response = client.post("/v1/origins/check", json={}, headers=_HEADERS)
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


# ----------------------------------------------------------------------
# Edges. Writing one here is what puts a host into the Ansible inventory:
# the `blitzecdn` plugin reads these same rows at the start of every run.
# ----------------------------------------------------------------------


_EDGE = {
    "name": "edge-01",
    "host": "192.0.2.10",
    "ssh_sources": ["198.51.100.0/24"],
}


def test_edge_crud_over_http(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/v1/edges", headers=_HEADERS).json() == []

        created = client.post("/v1/edges", json=_EDGE, headers=_HEADERS)
        assert created.status_code == 201
        assert created.json()["name"] == "edge-01"
        # Defaults are resolved by the model, not left for the plugin to guess.
        assert created.json()["user"] == "deploy"
        assert created.json()["port"] == 22

        assert client.post("/v1/edges", json=_EDGE, headers=_HEADERS).status_code == 409
        assert len(client.get("/v1/edges", headers=_HEADERS).json()) == 1
        assert (
            client.get("/v1/edges/edge-01", headers=_HEADERS).json()["host"]
            == "192.0.2.10"
        )
        assert client.get("/v1/edges/absent", headers=_HEADERS).status_code == 404

        patched = client.patch(
            "/v1/edges/edge-01",
            json={"port": 7845, "public_addresses": ["203.0.113.10"]},
            headers=_HEADERS,
        )
        assert patched.status_code == 200
        assert patched.json()["port"] == 7845
        assert patched.json()["public_addresses"] == ["203.0.113.10"]
        # Untouched fields survive a patch.
        assert patched.json()["ssh_sources"] == ["198.51.100.0/24"]

        assert (
            client.patch(
                "/v1/edges/absent", json={"port": 22}, headers=_HEADERS
            ).status_code
            == 404
        )


def test_an_edge_patch_cannot_rename_the_host(settings):
    """The name is how certificates, audit history and `--limit` refer to it.

    `EdgePatch` has no name field and forbids extras, so a rename is a 422 at
    the boundary rather than a silently ignored key — which would leave the
    caller believing a rename had happened.
    """
    with TestClient(create_app(settings)) as client:
        client.post("/v1/edges", json=_EDGE, headers=_HEADERS)

        response = client.patch(
            "/v1/edges/edge-01", json={"name": "edge-99"}, headers=_HEADERS
        )

        assert response.status_code == 422


def test_an_invalid_management_cidr_is_a_422_not_a_stored_edge(settings):
    """Validation is the model's, so the API rejects exactly what the CLI does."""
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/edges",
            json={**_EDGE, "ssh_sources": ["anywhere"]},
            headers=_HEADERS,
        )

        assert response.status_code == 422
        assert client.get("/v1/edges", headers=_HEADERS).json() == []


def test_removing_an_edge_without_decommissioning_says_so(settings):
    """The narrow case, and the response has to be honest about it.

    `decommission=false` leaves BlitzeCDN's configuration and every TLS private
    key on the host. A 204 could not distinguish that from a real teardown, so
    the body reports which one happened.
    """
    with TestClient(create_app(settings)) as client:
        client.post("/v1/edges", json=_EDGE, headers=_HEADERS)

        response = client.delete(
            "/v1/edges/edge-01?decommission=false", headers=_HEADERS
        )

        assert response.status_code == 200
        assert response.json() == {
            "name": "edge-01",
            "decommissioned": False,
            "hosts": [],
        }
        assert client.get("/v1/edges", headers=_HEADERS).json() == []


def test_a_failed_teardown_keeps_the_edge_registered(settings, monkeypatch):
    """Deregistering first would strand the host.

    The inventory is derived from these rows, so an edge removed before its
    teardown succeeded is a host no playbook can address again — still serving
    its last converged virtual hosts, still holding their private keys.
    """
    monkeypatch.setattr(
        EdgeOperationsService,
        "decommission_edge",
        lambda self, name, operator, force=False: (_ for _ in ()).throw(
            ExecutionError("teardown of edge-01 failed")
        ),
    )
    with TestClient(create_app(settings)) as client:
        client.post("/v1/edges", json=_EDGE, headers=_HEADERS)

        response = client.delete("/v1/edges/edge-01", headers=_HEADERS)

        assert response.status_code == 502
        assert [
            edge["name"] for edge in client.get("/v1/edges", headers=_HEADERS).json()
        ] == ["edge-01"]


def test_registering_an_edge_is_audited(settings):
    """Unanswerable before: edges never went through a service, so never a bus."""
    with TestClient(create_app(settings)) as client:
        client.post("/v1/edges", json=_EDGE, headers=_HEADERS)
        client.patch("/v1/edges/edge-01", json={"port": 7845}, headers=_HEADERS)

        events = client.get("/v1/audit-events", headers=_HEADERS).json()

        actions = [event["action"] for event in events]
        assert "edge.added" in actions
        assert "edge.updated" in actions
        update = next(event for event in events if event["action"] == "edge.updated")
        assert update["details"]["fields"] == ["port"]


def test_edge_routes_require_authentication(settings):
    """Every edge route is a control route; none may be read unauthenticated."""
    with TestClient(create_app(settings)) as client:
        assert client.get("/v1/edges").status_code == 401
        assert client.post("/v1/edges", json=_EDGE).status_code == 401
        assert client.patch("/v1/edges/edge-01", json={"port": 22}).status_code == 401
        assert client.delete("/v1/edges/edge-01").status_code == 401


def test_a_successful_teardown_reports_what_it_did(settings, monkeypatch):
    """The body carries the teardown result, which is why this is not a 204.

    During an incident "the edge is gone" is not the useful part — which tasks
    ran on which host is, and a forced removal that tore nothing down looks
    identical without it.
    """
    monkeypatch.setattr(
        EdgeOperationsService,
        "decommission_edge",
        lambda self, name, operator, force=False: (
            host_run("edge-01", changes=("remove /etc/blitzecdn",)),
        ),
    )
    with TestClient(create_app(settings)) as client:
        client.post("/v1/edges", json=_EDGE, headers=_HEADERS)

        response = client.delete("/v1/edges/edge-01", headers=_HEADERS)

        assert response.status_code == 200
        body = response.json()
        assert body["decommissioned"] is True
        assert [host["host"] for host in body["hosts"]] == ["edge-01"]
        assert body["hosts"][0]["changes"][0]["task"] == "remove /etc/blitzecdn"
        # Deliberately no assertion that the edge is gone: this stubs
        # `decommission_edge`, which owns the deregistration too, so such an
        # assertion would be checking the stub. That the teardown runs before
        # the removal is covered by the failure case above and by the service
        # tests.


def test_health_reports_unavailable_when_persistence_will_not_answer(
    settings, monkeypatch
):
    """A probe on uvicorn is not a probe on the control plane.

    It answered ok unconditionally, so a database gone unreadable — or one on a
    schema this release refuses — still reported healthy to whatever was
    watching. 503 rather than an error body: the caller reads the status.
    """
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200

        def refuse(_self, _limit=100):
            raise OSError("database is locked")

        monkeypatch.setattr(WorkflowStore, "list_workflows", refuse)
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"


def test_metrics_are_authenticated_and_readable(settings):
    """Gauges read out of SQLite at scrape time — no in-memory counters."""
    with TestClient(create_app(settings)) as client:
        assert client.get("/metrics").status_code == 401
        response = client.get("/metrics", headers=_HEADERS)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert "blitzecdn_edges 0" in body
    assert "blitzecdn_sites 0" in body
    assert "blitzecdn_certificates_expiring 0" in body
