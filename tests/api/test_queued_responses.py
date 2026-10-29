"""Clients can discover and poll every queued resource using the same headers."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from support.control_plane import (
    API_HEADERS,
    advance,
    control_plane_app,
    seed_edge,
    serve,
)

from blitzecdn.capabilities.deployments.domain import DeploymentStatus

SUBMISSIONS = [
    ("POST", "/v3/cache/purge", {"purge_all": True}, "operations"),
    ("POST", "/v3/cache/stats", {}, "operations"),
    ("POST", "/v3/origins/check", {}, "operations"),
    ("DELETE", "/v3/edges/{name}", None, "operations"),
    ("POST", "/v3/deployments", {}, "deployments"),
    ("POST", "/v3/drift", {}, "deployments"),
    ("POST", "/v3/rollbacks", {}, "deployments"),
]


@pytest.mark.parametrize(("method", "path", "body", "resource"), SUBMISSIONS)
def test_accepted_work_points_to_an_authenticated_polling_resource(
    settings, seeded, monkeypatch, method, path, body, resource
):
    control, _ = seeded()
    seed_edge(control)
    if path == "/v3/rollbacks":
        target = control.deployments.deploy("tester")
        body = {"deployment_id": target.id}
    serve(monkeypatch, control)

    with TestClient(control_plane_app(settings)) as client:
        accepted = client.request(
            method, path.format(name="edge-a"), json=body, headers=API_HEADERS
        )
        assert accepted.status_code == 202, accepted.text
        assert accepted.headers["Location"] == (
            f"/v3/{resource}/{accepted.json()['id']}"
        )
        assert accepted.headers["Retry-After"] == "2"
        assert accepted.headers["Cache-Control"] == "no-store"
        location = accepted.headers["Location"]
        assert client.get(location).status_code == 401
        polled = client.get(location, headers=API_HEADERS)
        assert polled.status_code == 200
        assert polled.json()["id"] == accepted.json()["id"]
        assert polled.json()["status"] in {"pending", "queued"}
        assert polled.headers["Retry-After"] == "2"
        assert polled.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("state", ["pending", "running", "succeeded", "failed"])
def test_operation_polling_stops_retry_hints_for_terminal_work(
    settings, seeded, monkeypatch, state
):
    control, repository = seeded()
    serve(monkeypatch, control)
    with TestClient(control_plane_app(settings)) as client:
        accepted = client.post("/v3/cache/stats", json={}, headers=API_HEADERS)
        job_id = accepted.json()["id"]
        if state != "pending":
            now = datetime.now(UTC)
            claim = repository.jobs.claim(worker="test", now=now, lease_seconds=900)
            assert claim is not None
            assert claim.job.id == job_id
            if state == "succeeded":
                assert repository.jobs.complete(
                    job_id, claim.fence, now=now, result={"edges": []}
                )
            elif state == "failed":
                assert repository.jobs.fail(
                    job_id, claim.fence, now=now, error="probe failed", retry_at=None
                )
        polled = client.get(accepted.headers["Location"], headers=API_HEADERS)
        assert polled.status_code == 200
        assert polled.json()["status"] == state
        assert ("Retry-After" in polled.headers) == (state in {"pending", "running"})
        assert polled.headers["Cache-Control"] == "no-store"
        if state == "failed":
            assert polled.json()["error"] == "probe failed"


@pytest.mark.parametrize("state", list(DeploymentStatus))
def test_deployment_polling_stops_retry_hints_for_every_terminal_state(
    settings, seeded, monkeypatch, state
):
    control, repository = seeded()
    seed_edge(control)
    serve(monkeypatch, control)
    with TestClient(control_plane_app(settings)) as client:
        accepted = client.post("/v3/deployments", json={}, headers=API_HEADERS)
        deployment_id = accepted.json()["id"]
        if state is not DeploymentStatus.QUEUED:
            advance(repository.deployments, deployment_id, DeploymentStatus.RUNNING)
            if state is not DeploymentStatus.RUNNING:
                advance(repository.deployments, deployment_id, state)
        polled = client.get(accepted.headers["Location"], headers=API_HEADERS)
        assert polled.status_code == 200
        assert polled.json()["status"] == state.value
        assert ("Retry-After" in polled.headers) == (
            state in {DeploymentStatus.QUEUED, DeploymentStatus.RUNNING}
        )
        assert polled.headers["Cache-Control"] == "no-store"


def test_openapi_documents_headers_for_all_accepted_and_polling_responses(settings):
    document = control_plane_app(settings).openapi()
    accepted = []
    for path, methods in document["paths"].items():
        for method, operation in methods.items():
            response = operation.get("responses", {}).get("202")
            if response is not None:
                accepted.append((method.upper(), path))
                assert set(response["headers"]) == {
                    "Location",
                    "Retry-After",
                    "Cache-Control",
                }
                assert response["headers"]["Retry-After"]["example"] == 2
    assert set(accepted) == {(method, path) for method, path, _, _ in SUBMISSIONS}
    for path in ("/v3/operations/{operation_id}", "/v3/deployments/{deployment_id}"):
        response = document["paths"][path]["get"]["responses"]["200"]
        assert set(response["headers"]) == {"Retry-After", "Cache-Control"}


def test_rejected_work_does_not_advertise_a_polling_resource(settings):
    with TestClient(control_plane_app(settings)) as client:
        for response, expected in (
            (client.post("/v3/cache/stats", json={}), 401),
            (client.post("/v3/cache/purge", json={}, headers=API_HEADERS), 409),
            (client.get("/v3/operations/missing", headers=API_HEADERS), 404),
        ):
            assert response.status_code == expected
            assert "Location" not in response.headers
            assert "Retry-After" not in response.headers
