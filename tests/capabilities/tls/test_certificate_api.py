"""This capability's HTTP surface: upload, metadata, renewal and preflight.

The helpers (``control_plane_app``, ``API_HEADERS``, ``seed_site_over_http``)
come from ``support.control_plane``.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from support.control_plane import (
    API_HEADERS,
    control_plane_app,
    run_queued_operations,
    seed_site_over_http,
    serve,
)

from blitzecdn.capabilities.tls.certificates.adapters.preflight import (
    CertificatePreflight,
)
from blitzecdn.capabilities.tls.certificates.domain import (
    PreflightCheck,
    PreflightReport,
    PreflightSeverity,
    ReconciliationResult,
    RenewalResult,
)
from blitzecdn.capabilities.tls.certificates.service import CertificateService
from blitzecdn.capabilities.tls.certificates.service.operations import (
    RECONCILE_OPERATION,
    RENEW_OPERATION,
    REQUEST_OPERATION,
    operation_handlers,
    operation_leases,
)


def _seed_proxied_record(client) -> None:
    seed_site_over_http(client, API_HEADERS)


def _stub_preflight(monkeypatch, *failures: str) -> None:
    """Replace the real checks, which would resolve names and probe an origin."""

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


def test_certificate_upload_and_metadata_api(settings, site_payload, certificate_pair):
    headers = {"X-API-Key": "x" * 32}
    certificate, key = certificate_pair()
    with TestClient(control_plane_app(settings)) as client:
        seed_site_over_http(client, headers)
        uploaded = client.post(
            "/v3/hosts/example-com/certificate/upload",
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
        metadata = client.get("/v3/hosts/example-com/certificate", headers=headers)
        assert metadata.json() == body


def test_certificates_are_readable(settings):
    # The origin check runs a playbook across the fleet, so it is a POST.
    with TestClient(control_plane_app(settings)) as client:
        assert client.get("/v3/certificates", headers=API_HEADERS).json()["items"] == []
        assert (
            client.get("/v3/certificates?expiring_in=30", headers=API_HEADERS).json()[
                "items"
            ]
            == []
        )


def test_automatic_ssl_reconciliation_is_exposed_by_the_api(settings):
    with TestClient(control_plane_app(settings)) as client:
        response = client.post(
            "/v3/ssl/automatic/reconcile",
            headers=API_HEADERS,
        )

    assert response.status_code == 200
    assert response.json() == {
        "scanned": [],
        "upgraded": {},
        "skipped": {},
        "deployment": None,
    }


def test_renewal_can_be_narrowed_to_named_sites(settings):
    with TestClient(control_plane_app(settings)) as client:
        response = client.post(
            "/v3/certificates/renew",
            json={"sites": ["absent-example-com"]},
            headers=API_HEADERS,
        )
        # No certificate by that name, so the selector is a 404 rather than an
        # empty success an automated caller would read as "nothing was due".
        assert response.status_code == 404


def test_an_empty_renewal_selector_is_a_422(settings):
    """`[]` would otherwise mean 'renew nothing' while reading as a filter."""
    with TestClient(control_plane_app(settings)) as client:
        response = client.post(
            "/v3/certificates/renew", json={"sites": []}, headers=API_HEADERS
        )
        assert response.status_code == 422


def test_renewal_is_bounded_by_the_configured_budget(monkeypatch):
    """A queued sweep must stop between sites before the worker's lease runs out."""
    seen: dict[str, object] = {}

    def renew(_self, operator, **kwargs):
        seen.update(kwargs, operator=operator)
        return RenewalResult()

    monkeypatch.setattr(CertificateService, "renew_certificates", renew)
    run = operation_handlers(
        CertificateService.__new__(CertificateService), renewal_budget_seconds=42
    )[RENEW_OPERATION]
    result = run(
        {"operator": "tester", "within_days": 30, "force": True, "sites": ["a"]}
    )

    assert result == {"renewed": [], "skipped": [], "failed": []}
    assert seen == {
        "operator": "tester",
        "within_days": 30,
        "force": True,
        "sites": ["a"],
        "budget_seconds": 42,
    }


def test_a_queued_sweep_is_leased_for_its_budget_and_one_more_issuance():
    """The budget is checked only between sites, so one issuance may overrun it."""
    assert operation_leases(
        renewal_budget_seconds=7200, issuance_timeout_seconds=1800
    ) == {
        REQUEST_OPERATION: 1800,
        RENEW_OPERATION: 9000,
        RECONCILE_OPERATION: 9000,
    }


def test_the_worker_holds_certificate_operations_for_their_declared_lease(
    settings, seeded
):
    control, _ = seeded()
    leases = control.worker.runner.operation_leases

    assert leases[RENEW_OPERATION] > control.worker.runner.queue.lease_seconds
    assert set(leases) >= {REQUEST_OPERATION, RENEW_OPERATION, RECONCILE_OPERATION}


def test_renewal_is_queued_rather_than_run_in_the_request(
    settings, seeded, monkeypatch
):
    """The CA is contacted by the worker, so no API thread waits on it."""
    calls: list[str] = []

    def renew(_self, operator, **_kwargs):
        calls.append(operator)
        return RenewalResult(skipped=("example-com: not due",))

    control, _ = seeded()
    serve(monkeypatch, control)
    monkeypatch.setattr(CertificateService, "renew_certificates", renew)
    with TestClient(control_plane_app(settings)) as client:
        accepted = client.post("/v3/certificates/renew", json={}, headers=API_HEADERS)
        assert accepted.status_code == 202, accepted.text
        assert accepted.json()["kind"] == "certificates.renew"
        assert calls == []

        # One sweep at a time: a second would only wait on the first.
        again = client.post("/v3/certificates/renew", json={}, headers=API_HEADERS)
        assert again.status_code == 409

        run_queued_operations(control)
        polled = client.get(accepted.headers["Location"], headers=API_HEADERS)

    assert len(calls) == 1
    assert polled.json()["status"] == "succeeded"
    assert polled.json()["result"] == {
        "renewed": [],
        "skipped": ["example-com: not due"],
        "failed": [],
    }


def test_reconciliation_is_queued_and_checks_every_waiting_site(
    settings, seeded, monkeypatch
):
    seen: dict[str, object] = {}

    def reconcile(_self, operator, *, force=False):
        seen["force"] = force
        return ReconciliationResult()

    control, _ = seeded()
    serve(monkeypatch, control)
    monkeypatch.setattr(CertificateService, "reconcile_certificates", reconcile)
    with TestClient(control_plane_app(settings)) as client:
        accepted = client.post("/v3/certificates/reconcile", headers=API_HEADERS)
        assert accepted.status_code == 202, accepted.text
        run_queued_operations(control)
        polled = client.get(accepted.headers["Location"], headers=API_HEADERS)

    assert seen == {"force": True}
    assert polled.json()["status"] == "succeeded"
    assert polled.json()["result"] == {
        "issued": [],
        "skipped": {},
        "failed": {},
        "deployment": None,
    }


def test_the_preflight_endpoint_reports_readiness(settings, monkeypatch):
    _stub_preflight(monkeypatch)
    with TestClient(control_plane_app(settings)) as client:
        _seed_proxied_record(client)

        response = client.get(
            "/v3/hosts/example-com/certificate/preflight", headers=API_HEADERS
        )

        assert response.status_code == 200
        assert response.json() == {"site": "example-com", "checks": []}


def test_the_preflight_endpoint_requires_auth(settings, monkeypatch):
    _stub_preflight(monkeypatch)
    with TestClient(control_plane_app(settings)) as client:
        assert (
            client.get("/v3/hosts/example-com/certificate/preflight").status_code == 401
        )


def test_a_blocked_preflight_fails_the_queued_request(settings, seeded, monkeypatch):
    """Preflight probes DNS and the origin, so it runs in the job, not the request."""
    _stub_preflight(monkeypatch, "dns")
    control, _ = seeded()
    serve(monkeypatch, control)
    with TestClient(control_plane_app(settings)) as client:
        accepted = client.post(
            "/v3/hosts/example-com/certificate/request",
            json={"email": "ops@example.com"},
            headers=API_HEADERS,
        )
        assert accepted.status_code == 202, accepted.text
        assert accepted.headers["Location"] == f"/v3/operations/{accepted.json()['id']}"

        # One request per site: a second would ask the CA again.
        again = client.post(
            "/v3/hosts/example-com/certificate/request",
            json={"email": "ops@example.com"},
            headers=API_HEADERS,
        )
        assert again.status_code == 409

        run_queued_operations(control)
        polled = client.get(accepted.headers["Location"], headers=API_HEADERS)

    assert polled.json()["status"] == "failed"
    assert "preflight failed" in polled.json()["error"]
    assert "Retry-After" not in polled.headers


def test_a_request_that_cannot_succeed_is_refused_before_it_is_queued(
    settings, seeded, monkeypatch
):
    control, _ = seeded()
    serve(monkeypatch, control)
    with TestClient(control_plane_app(settings)) as client:
        missing = client.post(
            "/v3/hosts/absent-example-com/certificate/request",
            json={"email": "ops@example.com"},
            headers=API_HEADERS,
        )
        no_email = client.post(
            "/v3/hosts/example-com/certificate/request",
            json={},
            headers=API_HEADERS,
        )

    assert missing.status_code == 404
    assert no_email.status_code == 409
    assert "BLITZE_ACME_DEFAULT_EMAIL" in no_email.json()["detail"]
    for refused in (missing, no_email):
        assert "Location" not in refused.headers


def test_skip_preflight_is_rejected_as_a_non_boolean(settings, monkeypatch):
    """`extra="forbid"` plus a typed field: a typo cannot silently disable this."""
    _stub_preflight(monkeypatch, "dns")
    with TestClient(control_plane_app(settings)) as client:
        _seed_proxied_record(client)

        assert (
            client.post(
                "/v3/hosts/example-com/certificate/request",
                json={"email": "ops@example.com", "skip_preflght": True},
                headers=API_HEADERS,
            ).status_code
            == 422
        )
