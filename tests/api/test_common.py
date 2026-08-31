import threading

from fastapi.testclient import TestClient

from blitzecdn.api import create_app
from blitzecdn.core.persistence.workflows import WorkflowStore


def test_create_app_defers_control_plane_io_until_lifespan(settings, monkeypatch):
    built = []

    def build(*_args, **_kwargs):
        built.append(True)
        raise AssertionError("construction must happen in lifespan")

    monkeypatch.setattr("blitzecdn.api.app.build_control_plane", build)
    create_app(settings)
    assert built == []


def test_health_is_public_and_controls_require_auth(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/metrics").status_code == 401
        assert client.get("/v1/sites").status_code == 401
        assert client.get("/v2/sites").status_code == 401
        wrong = client.get("/v1/sites", headers={"X-API-Key": "wrong"})
        assert wrong.status_code == 401
        assert wrong.headers["WWW-Authenticate"] == "ApiKey"
        assert (
            client.get("/v1/sites", headers={"X-API-Key": "x" * 32}).status_code == 200
        )


def test_health_reports_redis_unavailable(settings, monkeypatch):
    monkeypatch.setattr("blitzecdn.bootstrap.redis_ready", lambda _url: False)
    with TestClient(create_app(settings)) as client:
        response = client.get("/health")
    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "check": "broker",
        "detail": "ConnectionError",
    }


def test_api_service_runs_certificate_reconciliation_on_its_interval(
    settings, monkeypatch
):
    called = threading.Event()
    configured = settings.model_copy(
        update={"certificate_reconcile_interval_seconds": 1}
    )

    def enqueue(_url, job, *, ttl_seconds):
        assert job == "certificate-reconciliation"
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

    def enqueue(_url, job, *, ttl_seconds):
        assert job == "automatic-ssl-scan"
        assert ttl_seconds >= 2
        called.set()
        return True

    monkeypatch.setattr("blitzecdn.scheduler.enqueue_scheduled_once", enqueue)

    with TestClient(create_app(configured)):
        assert called.wait(2)


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


def test_api_fails_closed_without_keys(settings):
    insecure = settings.model_copy(update={"api_keys": {}})
    with TestClient(create_app(insecure)) as client:
        assert client.get("/v1/sites").status_code == 503


_HEADERS = {"X-API-Key": "x" * 32}


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
