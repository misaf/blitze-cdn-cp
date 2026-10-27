"""`/v3/cache/*`, over the real application.

Built with `create_app`, so a route that stopped being contributed would 404.
"""

from fastapi.testclient import TestClient
from support.control_plane import (
    FakeRunner,
    ansible_run,
    host_run,
    run_queued_operations,
    serve,
)

from blitzecdn.entrypoints.api import create_app

_HEADERS = {"X-API-Key": "x" * 32}


def _proxied_site(client, domain_payload, record_payload):
    client.post("/v3/domains", json=domain_payload, headers=_HEADERS)
    client.post(
        "/v3/domains/example.com/records",
        json={**record_payload, "proxied": True},
        headers=_HEADERS,
    )
    return client.get("/v3/hosts", headers=_HEADERS).json()[0]["server_names"][0]


def test_purging_a_hostname_no_site_serves_is_a_404(settings):
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v3/cache/purge",
            json={"entries": [{"host": "nothing.example.com", "uri": "/x"}]},
            headers=_HEADERS,
        )
        assert response.status_code == 404


def test_an_empty_purge_request_is_a_409(settings):
    """Neither entries nor purge_all: refused rather than reported as done."""
    with TestClient(create_app(settings)) as client:
        response = client.post("/v3/cache/purge", json={}, headers=_HEADERS)
        assert response.status_code == 409


def test_purging_everything_and_entries_at_once_is_a_409(settings):
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v3/cache/purge",
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
            "/v3/cache/purge",
            json={"entries": [{"host": "cdn.example.com", "uri": "app.js"}]},
            headers=_HEADERS,
        )
        assert response.status_code == 422


def test_a_purge_host_limit_that_could_widen_is_a_422(settings):
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v3/cache/purge",
            json={"purge_all": True, "host_limit": "edge-a:!edge-b"},
            headers=_HEADERS,
        )
        assert response.status_code == 422


def _purge(client):
    return client.post(
        "/v3/cache/purge",
        # The derived site has no certificate, so it serves http and only an
        # http entry could have been cached.
        json={
            "entries": [{"host": "cdn.example.com", "uri": "/app.js", "scheme": "http"}]
        },
        headers=_HEADERS,
    )


def test_a_purge_is_queued_and_its_result_is_read_from_the_operation(
    settings, seeded, monkeypatch
):
    """202 at once; the worker runs it and the operation carries the PurgeResult."""
    control, _ = seeded(FakeRunner([ansible_run(host_run("edge-a"))]))
    serve(monkeypatch, control)

    with TestClient(create_app(settings)) as client:
        queued = _purge(client)
        assert queued.status_code == 202
        assert queued.json()["status"] == "pending"
        assert queued.json()["kind"] == "cache.purge"

        run_queued_operations(control)
        operation = client.get(
            f"/v3/operations/{queued.json()['id']}", headers=_HEADERS
        ).json()

    assert operation["status"] == "succeeded"
    assert operation["result"]["complete"] is True
    assert operation["result"]["failed_hosts"] == []


def test_a_partial_purge_still_carries_the_whole_result(settings, seeded, monkeypatch):
    """The dangerous outcome has to stay machine-readable.

    The operation succeeds, since it ran; its result names the edges that may
    still serve the object.
    """
    control, _ = seeded(
        FakeRunner(
            [ansible_run(host_run("edge-a"), host_run("edge-b", failure="rm failed"))]
        )
    )
    serve(monkeypatch, control)

    with TestClient(create_app(settings)) as client:
        queued = _purge(client)
        run_queued_operations(control)
        result = client.get(
            f"/v3/operations/{queued.json()['id']}", headers=_HEADERS
        ).json()["result"]

    assert result["complete"] is False
    assert result["failed_hosts"] == ["edge-b"]
    assert {host["host"] for host in result["hosts"]} == {"edge-a", "edge-b"}


def test_the_purge_result_keeps_its_shape(settings, seeded, monkeypatch):
    """The report an operator's tooling reads from the operation, pinned."""
    control, _ = seeded(FakeRunner([ansible_run(host_run("edge-a"))]))
    serve(monkeypatch, control)

    with TestClient(create_app(settings)) as client:
        queued = _purge(client)
        run_queued_operations(control)
        result = client.get(
            f"/v3/operations/{queued.json()['id']}", headers=_HEADERS
        ).json()["result"]

    assert " ".join(sorted(result)) == (
        "complete entries failed_hosts host_limit hosts purge_all purged_at"
    )


def test_a_deployment_job_is_not_an_operation(settings, seeded, monkeypatch):
    """Only declared edge operations are readable; the queue stays internal."""
    control, _ = seeded(FakeRunner())
    job = control.job_queue.enqueue_deployment("d-1")
    assert job is not None
    serve(monkeypatch, control)

    with TestClient(create_app(settings)) as client:
        response = client.get(f"/v3/operations/{job.id}", headers=_HEADERS)

    assert response.status_code == 404
