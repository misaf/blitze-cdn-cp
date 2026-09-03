"""`/v1/cache/*`, over the real application.

Built with `create_app` rather than the core-only app the control plane's own
API tests use: these routes exist *because* this distribution is installed, so
the app under test is the one plugin discovery assembles. That is also what
makes them a check on the packaging and not only on the handlers — a route that
stopped being contributed would 404 here.
"""

from control_plane_fixtures import FakeRunner, ansible_run, host_run
from fastapi.testclient import TestClient

from blitzecdn.api import create_app

_HEADERS = {"X-API-Key": "x" * 32}


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
        "blitzecdn.api.app.build_control_plane", lambda _settings, **_kwargs: control
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
        "blitzecdn.api.app.build_control_plane", lambda _settings, **_kwargs: control
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


def test_the_published_purge_result_shape_is_pinned(settings):
    """The operational shape this capability publishes, pinned.

    It was in the control plane's shape table while `cache` was a package
    inside it. It is owned by the distribution that publishes it now, and
    pinned here for the same reason it was pinned there.
    """
    with TestClient(create_app(settings)) as client:
        schemas = client.get("/openapi.json").json()["components"]["schemas"]

    assert " ".join(sorted(schemas["PurgeResult"]["properties"])) == (
        "complete entries failed_hosts host_limit hosts purge_all purged_at"
    )
    assert " ".join(sorted(schemas["PurgeResult"]["required"])) == (
        "complete failed_hosts purged_at"
    )
