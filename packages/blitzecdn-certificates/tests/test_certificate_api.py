"""This capability's HTTP surface: upload, metadata, renewal and preflight.

Every test here was in ``tests/api/test_api.py``, held there by name in a
``REQUIRES_CERTIFICATES`` set that the shared fixtures read in order to skip
them whenever this wheel was detached. The set was a register of tests that had
not followed their code out of core, and moving them is what it was standing in
for: these routes exist only while this distribution is installed, so the
suite that asserts on them belongs to the distribution.

The helpers they need — ``control_plane_app``, ``API_HEADERS`` and
``seed_site_over_http`` — come from ``control_plane_fixtures``, which the
workspace's root ``conftest.py`` registers as a plugin for every distribution's
tests.
"""

from __future__ import annotations

import threading

from blitzecdn_certificates.certificates.adapters.preflight import CertificatePreflight
from blitzecdn_certificates.certificates.domain import (
    PreflightCheck,
    PreflightReport,
    PreflightSeverity,
)
from blitzecdn_certificates.certificates.service import CertificateService
from control_plane_fixtures import (
    API_HEADERS,
    control_plane_app,
    seed_site_over_http,
    with_capability_settings,
)
from fastapi.testclient import TestClient


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


def test_certificates_are_readable(settings):
    # Origins are no longer among these: the check runs a playbook across the
    # fleet now, so it is a POST like the cache-statistics route rather than a
    # read the controller can answer by itself.
    with TestClient(control_plane_app(settings)) as client:
        assert client.get("/v1/certificates", headers=API_HEADERS).json() == []
        assert (
            client.get("/v1/certificates?expiring_in=30", headers=API_HEADERS).json()
            == []
        )
        assert (
            client.post("/v1/certificates/renew", json={}, headers=API_HEADERS).json()[
                "renewed"
            ]
            == []
        )


def test_automatic_ssl_reconciliation_is_exposed_by_the_api(settings):
    with TestClient(control_plane_app(settings)) as client:
        response = client.post(
            "/v1/ssl/automatic/reconcile",
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
            "/v1/certificates/renew",
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
            "/v1/certificates/renew", json={"sites": []}, headers=API_HEADERS
        )
        assert response.status_code == 422


def test_renewal_is_bounded_by_the_configured_budget(settings, monkeypatch):
    """A sweep served over HTTP must not run until the CA gets bored."""
    seen: dict[str, object] = {}

    def renew(_self, operator, **kwargs):
        seen.update(kwargs)
        seen["operator"] = operator
        return {"renewed": [], "skipped": [], "failed": []}

    configured = with_capability_settings(
        settings, certificate_renewal_budget_seconds=42
    )
    monkeypatch.setattr(CertificateService, "renew_certificates", renew)
    with TestClient(control_plane_app(configured)) as client:
        response = client.post("/v1/certificates/renew", json={}, headers=API_HEADERS)

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
    with TestClient(control_plane_app(settings)) as client:
        assert (
            client.post(
                "/v1/certificates/renew", json={}, headers=API_HEADERS
            ).status_code
            == 200
        )

    assert names and names[0].startswith("blitzecdn-worker")


def test_the_preflight_endpoint_reports_readiness(settings, monkeypatch):
    _stub_preflight(monkeypatch)
    with TestClient(control_plane_app(settings)) as client:
        _seed_proxied_record(client)

        response = client.get(
            "/v1/sites/cdn-example-com/certificate/preflight", headers=API_HEADERS
        )

        assert response.status_code == 200
        assert response.json() == {"site": "cdn-example-com", "checks": []}


def test_the_preflight_endpoint_requires_auth(settings, monkeypatch):
    _stub_preflight(monkeypatch)
    with TestClient(control_plane_app(settings)) as client:
        assert (
            client.get("/v1/sites/cdn-example-com/certificate/preflight").status_code
            == 401
        )


def test_a_blocked_preflight_makes_a_request_a_409(settings, monkeypatch):
    _stub_preflight(monkeypatch, "dns")
    with TestClient(control_plane_app(settings)) as client:
        _seed_proxied_record(client)

        response = client.post(
            "/v1/sites/cdn-example-com/certificate/request",
            json={"email": "ops@example.com"},
            headers=API_HEADERS,
        )

        assert response.status_code == 409
        assert "preflight failed" in response.json()["detail"]


def test_skip_preflight_is_rejected_as_a_non_boolean(settings, monkeypatch):
    """`extra="forbid"` plus a typed field: a typo cannot silently disable this."""
    _stub_preflight(monkeypatch, "dns")
    with TestClient(control_plane_app(settings)) as client:
        _seed_proxied_record(client)

        assert (
            client.post(
                "/v1/sites/cdn-example-com/certificate/request",
                json={"email": "ops@example.com", "skip_preflght": True},
                headers=API_HEADERS,
            ).status_code
            == 422
        )
