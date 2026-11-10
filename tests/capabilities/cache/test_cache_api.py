"""`/v3/cache/*`, over the real application.

Built with `create_app`, so a route that stopped being contributed would 404.
"""

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from support.control_plane import (
    FakeRunner,
    ansible_run,
    host_run,
    run_queued_operations,
    serve,
)

from blitzecdn.capabilities.traffic.domain import (
    CollectedUsage,
    HourlyUsage,
    LogCursor,
    UsageBatch,
)
from blitzecdn.entrypoints.api import create_app

_HEADERS = {"X-API-Key": "x" * 32}


def _proxied_site(client, domain_payload, record_create_payload):
    client.post("/v3/domains", json=domain_payload, headers=_HEADERS)
    client.post(
        "/v3/domains/example.com/records",
        json={**record_create_payload, "proxied": True},
        headers=_HEADERS,
    )
    return client.get("/v3/sites", headers=_HEADERS).json()["items"][0]["server_names"][
        0
    ]


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


def test_a_purge_prefix_that_is_not_a_path_is_a_422(settings):
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v3/cache/purge",
            json={"prefixes": [{"host": "cdn.example.com", "prefix": "blog/"}]},
            headers=_HEADERS,
        )
        assert response.status_code == 422


def test_a_purge_tag_with_a_comma_is_a_422(settings):
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v3/cache/purge", json={"tags": ["a,b"]}, headers=_HEADERS
        )
        assert response.status_code == 422


def test_more_than_fifty_purge_tags_is_a_422(settings):
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v3/cache/purge",
            json={"tags": [f"t{n}" for n in range(51)]},
            headers=_HEADERS,
        )
        assert response.status_code == 422


def test_a_tag_purge_is_queued_and_carries_its_tags(settings, seeded, monkeypatch):
    control, _ = seeded(FakeRunner([ansible_run(host_run("edge-a"))]))
    serve(monkeypatch, control)

    with TestClient(create_app(settings)) as client:
        queued = client.post(
            "/v3/cache/purge", json={"tags": ["Product-42"]}, headers=_HEADERS
        )
        assert queued.status_code == 202
        run_queued_operations(control)
        result = client.get(
            f"/v3/operations/{queued.json()['id']}", headers=_HEADERS
        ).json()["result"]

    assert result["complete"] is True
    assert result["tags"] == ["product-42"]


def test_a_hostname_purge_is_queued_and_carries_its_prefixes(
    settings, seeded, monkeypatch
):
    control, _ = seeded(FakeRunner([ansible_run(host_run("edge-a"))]))
    serve(monkeypatch, control)

    with TestClient(create_app(settings)) as client:
        queued = client.post(
            "/v3/cache/purge",
            json={"prefixes": [{"host": "cdn.example.com"}]},
            headers=_HEADERS,
        )
        assert queued.status_code == 202
        run_queued_operations(control)
        result = client.get(
            f"/v3/operations/{queued.json()['id']}", headers=_HEADERS
        ).json()["result"]

    assert result["complete"] is True
    assert result["prefixes"] == [{"host": "cdn.example.com", "prefix": "/"}]


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
        "complete entries failed_hosts host_limit hosts prefixes purge_all purged_at"
        " tags"
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


def _seed_cache_hour(repository):
    repository.traffic.apply(
        "edge-a",
        CollectedUsage(
            batches=(
                UsageBatch(
                    end=LogCursor(stream_id="a" * 64, offset=100, anchor="f" * 64),
                    buckets=(
                        HourlyUsage(
                            hostname="cdn.example.com",
                            hour=datetime(2026, 10, 27, 9, tzinfo=UTC),
                            requests=5,
                            bytes_sent=50,
                            body_bytes_sent=0,
                            cache={
                                "HIT": {"requests": 3, "bytes_sent": 30},
                                "MISS": {"requests": 1, "bytes_sent": 10},
                                "NONE": {"requests": 1, "bytes_sent": 10},
                            },
                        ),
                    ),
                ),
            )
        ),
    )


def test_stored_cache_stats_are_read_without_queueing(settings, seeded, monkeypatch):
    fake = FakeRunner()
    control, repository = seeded(fake)
    _seed_cache_hour(repository)
    serve(monkeypatch, control)
    params = {"start": "2026-10-27T09:00:00Z", "end": "2026-10-27T10:00:00Z"}

    with TestClient(create_app(settings)) as client:
        assert client.get("/v3/cache/stats", params=params).status_code == 401
        response = client.get("/v3/cache/stats", params=params, headers=_HEADERS)
        invalid = client.get(
            "/v3/cache/stats",
            params={**params, "end": params["start"]},
            headers=_HEADERS,
        )

    assert response.status_code == 200, response.text
    page = response.json()
    assert page["total"] == 1
    (row,) = page["items"]
    assert row["requests"] == 5
    assert row["cacheable_requests"] == 4
    assert row["hit_ratio"] == 0.75
    assert row["cached_bytes_sent"] == 30
    assert row["byte_hit_ratio"] == 0.75
    assert row["outcomes"]["NONE"] == {"requests": 1, "bytes_sent": 10}
    assert invalid.status_code == 422
    assert all(error["loc"][0] == "query" for error in invalid.json()["detail"])
    assert fake.playbooks == []


def test_connection_counters_are_a_queued_operation(settings, seeded, monkeypatch):
    report = {"nginx_reachable": True, "connections": {"active": 2}}
    control, _ = seeded(FakeRunner([ansible_run(host_run("edge-a", report=report))]))
    serve(monkeypatch, control)

    with TestClient(create_app(settings)) as client:
        queued = client.post("/v3/cache/connections", json={}, headers=_HEADERS)
        assert queued.status_code == 202
        assert queued.json()["kind"] == "cache.connections"
        run_queued_operations(control)
        result = client.get(
            f"/v3/operations/{queued.json()['id']}", headers=_HEADERS
        ).json()["result"]

    assert result["edges"][0]["connections"] == {"active": 2}
