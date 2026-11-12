"""A queued request repeated with its ``Idempotency-Key`` is answered, not re-queued."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from support.control_plane import API_HEADERS, control_plane_app, seed_edge, serve

from blitzecdn.core.persistence.idempotency import IdempotencyKeys
from blitzecdn.delivery.api.idempotency import _fingerprint

#: Every queued route, with a body it admits.
SUBMISSIONS = [
    ("POST", "/v3/cache/purge", {"purge_all": True}),
    ("POST", "/v3/cache/connections", {}),
    ("POST", "/v3/origins/check", {}),
    ("DELETE", "/v3/edges/edge-a", None),
    ("POST", "/v3/deployments", {}),
    ("POST", "/v3/deployments", {"check": True}),
    ("POST", "/v3/deployments/rollback", {}),
    ("POST", "/v3/certificates/renew", {}),
    ("POST", "/v3/certificates/reconcile", None),
    ("POST", "/v3/sites/example-com/certificate/request", {"email": "ops@example.com"}),
]


def _keyed(key: str) -> dict[str, str]:
    return {**API_HEADERS, "Idempotency-Key": key}


@pytest.mark.parametrize(("method", "path", "body"), SUBMISSIONS)
def test_a_repeated_key_returns_the_first_resource_and_queues_nothing(
    settings, seeded, monkeypatch, method, path, body
):
    control, repository = seeded()
    seed_edge(control)
    if path == "/v3/deployments/rollback":
        body = {"deployment_id": control.deployments.deploy("tester").id}
    serve(monkeypatch, control)

    with TestClient(control_plane_app(settings)) as client:
        first = client.request(method, path, json=body, headers=_keyed("retry-1"))
        assert first.status_code == 202, first.text
        queued = len(repository.jobs.list_jobs(limit=1000))

        again = client.request(method, path, json=body, headers=_keyed("retry-1"))

        assert again.status_code == 202, again.text
        assert again.json()["id"] == first.json()["id"]
        assert again.headers["Location"] == first.headers["Location"]
        assert again.headers["Retry-After"] == "2"
        assert len(repository.jobs.list_jobs(limit=1000)) == queued


def test_without_a_key_each_request_queues_its_own_work(settings, seeded, monkeypatch):
    control, _ = seeded()
    serve(monkeypatch, control)
    with TestClient(control_plane_app(settings)) as client:
        first = client.post("/v3/cache/connections", json={}, headers=API_HEADERS)
        second = client.post("/v3/cache/connections", json={}, headers=API_HEADERS)
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] != second.json()["id"]


def test_a_key_reused_for_a_different_request_is_refused(settings, seeded, monkeypatch):
    control, _ = seeded()
    serve(monkeypatch, control)
    with TestClient(control_plane_app(settings)) as client:
        first = client.post("/v3/cache/connections", json={}, headers=_keyed("reused"))
        other = client.post(
            "/v3/cache/connections",
            json={"host_limit": "edge-a"},
            headers=_keyed("reused"),
        )
        elsewhere = client.post("/v3/origins/check", json={}, headers=_keyed("reused"))
    assert first.status_code == 202
    assert other.status_code == elsewhere.status_code == 409
    assert "different request" in other.json()["detail"]
    assert "Location" not in other.headers


def test_a_json_body_matches_by_value_not_by_spelling(settings, seeded, monkeypatch):
    control, _ = seeded()
    serve(monkeypatch, control)
    headers = {**_keyed("spelling"), "Content-Type": "application/json"}
    with TestClient(control_plane_app(settings)) as client:
        first = client.post(
            "/v3/cache/purge", content=b'{"purge_all": true}', headers=headers
        )
        again = client.post(
            "/v3/cache/purge", content=b'{ "purge_all":true }', headers=headers
        )
    assert first.status_code == again.status_code == 202
    assert again.json()["id"] == first.json()["id"]


def test_a_refused_request_leaves_its_key_free_for_a_retry(
    settings, seeded, monkeypatch
):
    control, _ = seeded()
    serve(monkeypatch, control)
    with TestClient(control_plane_app(settings)) as client:
        refused = client.post("/v3/cache/purge", json={}, headers=_keyed("free"))
        assert refused.status_code == 409
        # The same request again is checked again, not answered from the key.
        assert (
            client.post("/v3/cache/purge", json={}, headers=_keyed("free")).status_code
            == 409
        )
        accepted = client.post(
            "/v3/cache/purge", json={"purge_all": True}, headers=_keyed("free")
        )
    assert accepted.status_code == 202


def test_a_key_still_being_admitted_answers_conflict(settings, seeded, monkeypatch):
    control, repository = seeded()
    serve(monkeypatch, control)
    # Reserved and not yet bound, as by a first try still being admitted.
    fingerprint = _fingerprint("POST", "/v3/cache/connections", "", b"{}")
    assert (
        repository.idempotency_keys.reserve("tester", "in-flight", fingerprint) is None
    )
    with TestClient(control_plane_app(settings)) as client:
        busy = client.post(
            "/v3/cache/connections", json={}, headers=_keyed("in-flight")
        )
    assert busy.status_code == 409
    assert "still being admitted" in busy.json()["detail"]


@pytest.mark.parametrize("key", ["", "has space", "x" * 256, "naïve"])
def test_a_malformed_key_is_a_validation_error(settings, seeded, monkeypatch, key):
    control, _ = seeded()
    serve(monkeypatch, control)
    with TestClient(control_plane_app(settings)) as client:
        response = client.post(
            "/v3/cache/connections",
            json={},
            headers={**API_HEADERS, "Idempotency-Key": key.encode()},
        )
    assert response.status_code == 422


def test_openapi_documents_the_header_on_every_queued_route(settings):
    document = control_plane_app(settings).openapi()
    for path, methods in document["paths"].items():
        for operation in methods.values():
            parameters = {
                parameter["name"] for parameter in operation.get("parameters", [])
            }
            queued = "202" in operation.get("responses", {})
            assert ("Idempotency-Key" in parameters) == queued, path


def test_expired_keys_and_abandoned_reservations_reserve_afresh(seeded):
    _, repository = seeded()
    database = repository.database
    keys = IdempotencyKeys(database)
    keys.reserve("tester", "kept", "a")
    keys.bind("tester", "kept", "job-1")
    assert keys.reserve("tester", "kept", "a").resource_id == "job-1"
    # Keys are per operator.
    assert keys.reserve("someone-else", "kept", "a") is None

    expired = IdempotencyKeys(database, lifetime=timedelta(seconds=-1))
    assert expired.reserve("tester", "kept", "a") is None

    keys.reserve("tester", "stuck", "a")
    assert keys.reserve("tester", "stuck", "a").resource_id is None
    abandoned = IdempotencyKeys(database, reservation_timeout=timedelta(seconds=-1))
    assert abandoned.reserve("tester", "stuck", "a") is None
