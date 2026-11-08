"""Per-site analytics: attribution, dense buckets, and both public interfaces."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from support.cli import runner
from support.control_plane import (
    API_HEADERS,
    cli_control_plane,
    control_plane_app,
    serve,
)

from blitzecdn.capabilities.analytics.domain import (
    AnalyticsBucket,
    Granularity,
    PathsQuery,
    SiteQuery,
    owned_hostnames,
    owner,
)
from blitzecdn.capabilities.analytics.service import AnalyticsService
from blitzecdn.capabilities.traffic.domain import (
    CollectedUsage,
    HourlyUsage,
    LogCursor,
    UsageBatch,
)
from blitzecdn.composition import Repository
from blitzecdn.core.domain.site import CdnSite
from blitzecdn.core.exceptions import NotFoundError
from blitzecdn.entrypoints.cli import main as cli

DAY = datetime(2026, 10, 27, tzinfo=UTC)


def _site(name, *server_names):
    return CdnSite(name=name, server_names=server_names, origin_host="example.net")


SITES = (
    _site("apex", "example.com"),
    _site("wild", "*.example.com"),
    _site("deep", "*.eu.example.com"),
    _site("pinned", "api.eu.example.com"),
)


class Sites:
    def __init__(self, sites=SITES):
        self._sites = list(sites)

    def list_sites(self):
        return self._sites

    def get_site(self, name):
        for site in self._sites:
            if site.name == name:
                return site
        raise NotFoundError(name)


@pytest.mark.parametrize(
    ("hostname", "expected"),
    [
        ("example.com", "apex"),
        ("www.example.com", "wild"),
        ("a.b.example.com", "wild"),
        ("cdn.eu.example.com", "deep"),
        ("api.eu.example.com", "pinned"),
        ("example.org", None),
        ("badexample.com", None),
    ],
)
def test_hostnames_are_attributed_as_nginx_would(hostname, expected):
    assert owner(hostname, SITES) == expected


def test_a_wildcard_site_owns_only_the_hostnames_it_wins():
    seen = {"www.example.com", "cdn.eu.example.com", "api.eu.example.com", "x.org"}
    assert owned_hostnames(SITES[1], SITES, seen) == {"www.example.com"}
    assert owned_hostnames(SITES[3], SITES, ()) == {"api.eu.example.com"}


@pytest.mark.parametrize(
    "fields",
    [
        {"start": "2026-10-27T00:00:00"},
        {"start": "2026-10-27T00:30:00Z"},
        {"end": "2026-10-27T00:00:00Z"},
        {"granularity": "day", "end": "2026-10-28T05:00:00Z"},
        {"granularity": "week"},
        {"limit": 501},
    ],
)
def test_invalid_queries_are_refused(fields):
    with pytest.raises(ValidationError):
        SiteQuery.model_validate(
            {
                "site": "wild",
                "start": "2026-10-27T00:00:00Z",
                "end": "2026-10-29T00:00:00Z",
                **fields,
            }
        )


def _bucket(hostname, hour, requests, *, hits, errors=0, paths=()):
    status = {"2xx": requests - errors}
    if errors:
        status["5xx"] = errors
    cache = {"HIT": {"requests": hits, "bytes_sent": hits * 10}} if hits else {}
    if requests - hits:
        cache["NONE" if not hits else "MISS"] = {
            "requests": requests - hits,
            "bytes_sent": (requests - hits) * 10,
        }
    return HourlyUsage(
        hostname=hostname,
        hour=hour,
        requests=requests,
        bytes_sent=requests * 10,
        body_bytes_sent=requests * 8,
        cache=cache,
        status={key: value for key, value in status.items() if value},
        paths=tuple(
            {"path": path, "requests": count, "bytes_sent": count * 10}
            for path, count in paths
        ),
    )


def seed(store):
    store.apply(
        "edge-a",
        CollectedUsage(
            batches=(
                UsageBatch(
                    end=LogCursor(stream_id="a" * 64, offset=100, anchor="b" * 64),
                    buckets=(
                        _bucket(
                            "www.example.com",
                            DAY + timedelta(hours=1),
                            10,
                            hits=6,
                            errors=2,
                            paths=[("/index.html", 7), ("/app.js", 3)],
                        ),
                        _bucket(
                            "cdn.eu.example.com",
                            DAY + timedelta(hours=1),
                            50,
                            hits=50,
                            paths=[("/elsewhere", 50)],
                        ),
                        _bucket(
                            "img.example.com",
                            DAY + timedelta(hours=26),
                            4,
                            hits=0,
                            paths=[("/app.js", 4)],
                        ),
                    ),
                ),
            )
        ),
    )
    store.apply(
        "edge-b",
        CollectedUsage(
            batches=(
                UsageBatch(
                    end=LogCursor(stream_id="c" * 64, offset=100, anchor="d" * 64),
                    buckets=(
                        _bucket(
                            "www.example.com",
                            DAY + timedelta(hours=1),
                            10,
                            hits=4,
                            paths=[("/index.html", 10)],
                        ),
                    ),
                ),
            )
        ),
    )


@pytest.fixture
def analytics(settings):
    repository = Repository(settings.database_path)
    seed(repository.traffic)
    yield AnalyticsService(sites=Sites(), traffic=repository.traffic)
    repository.close()


def test_hourly_series_is_dense_and_summed_over_edges_and_hostnames(analytics):
    query = SiteQuery(site="wild", start=DAY, end=DAY + timedelta(hours=3))

    page = analytics.series(query)

    assert page.total == 3
    empty, busy, after = page.items
    assert empty == AnalyticsBucket(start=DAY)
    assert after.requests == 0
    assert after.hit_ratio is None
    assert busy.start == DAY + timedelta(hours=1)
    # The deeper wildcard's hostname is not this site's.
    assert busy.requests == 20
    assert busy.bytes_sent == 200
    assert busy.body_bytes_sent == 160
    assert busy.cacheable_requests == 20
    assert busy.hits == 10
    assert busy.hit_ratio == 0.5
    assert busy.status == {"1xx": 0, "2xx": 18, "3xx": 0, "4xx": 0, "5xx": 2}
    assert busy.server_error_ratio == 0.1
    assert busy.client_error_ratio == 0.0


def test_daily_buckets_and_pages(analytics):
    query = SiteQuery(
        site="wild",
        start=DAY,
        end=DAY + timedelta(days=3),
        granularity=Granularity.DAY,
        limit=2,
        offset=0,
    )
    first = analytics.series(query)
    assert first.total == 3
    assert [(item.start, item.requests) for item in first.items] == [
        (DAY, 20),
        (DAY + timedelta(days=1), 4),
    ]
    (last,) = analytics.series(query.model_copy(update={"offset": 2})).items
    assert last == AnalyticsBucket(start=DAY + timedelta(days=2))
    beyond = analytics.series(query.model_copy(update={"offset": 9}))
    assert (beyond.items, beyond.total) == ([], 3)


def test_top_paths_rank_the_sites_own_hostnames(analytics):
    page = analytics.top_paths(
        PathsQuery(site="wild", start=DAY, end=DAY + timedelta(days=2))
    )
    assert [(item.path, item.requests) for item in page.items] == [
        ("/index.html", 17),
        ("/app.js", 7),
    ]
    assert page.total == 2


def test_unknown_sites_are_not_found(analytics):
    query = SiteQuery(site="nope", start=DAY, end=DAY + timedelta(hours=1))
    with pytest.raises(NotFoundError):
        analytics.series(query)
    with pytest.raises(NotFoundError):
        analytics.series(query.model_copy(update={"offset": 5}))
    with pytest.raises(NotFoundError):
        analytics.top_paths(
            PathsQuery(site="nope", start=DAY, end=DAY + timedelta(hours=1))
        )


def test_an_exact_site_never_scans_hostnames(settings):
    repository = Repository(settings.database_path)
    seed(repository.traffic)

    class Counting:
        scans = 0

        def __getattr__(self, name):
            return getattr(repository.traffic, name)

        def hostnames(self, **window):
            Counting.scans += 1
            return repository.traffic.hostnames(**window)

    service = AnalyticsService(
        sites=Sites((_site("one", "www.example.com"),)), traffic=Counting()
    )
    (bucket,) = service.series(
        SiteQuery(
            site="one",
            start=DAY + timedelta(hours=1),
            end=DAY + timedelta(hours=2),
        )
    ).items
    assert bucket.requests == 20
    assert Counting.scans == 0
    repository.close()


@pytest.fixture
def client(settings, monkeypatch):
    control = cli_control_plane(settings, monkeypatch)
    assert runner.invoke(cli.app, ["domain", "add", "example.com"]).exit_code == 0
    command = runner.invoke(
        cli.app,
        ["record", "add", "example.com", "www", "--value", "198.51.100.20", "--proxy"],
    )
    assert command.exit_code == 0, command.output
    seed(control.container.get(Repository).traffic)
    serve(monkeypatch, control)
    with TestClient(control_plane_app(settings)) as client:
        yield client
    control.container.close()


def test_api_and_cli_agree(client):
    sites = client.get("/v3/hosts", headers=API_HEADERS).json()["items"]
    (site,) = [item for item in sites if "www.example.com" in item["server_names"]]
    params = {
        "start": "2026-10-27T00:00:00Z",
        "end": "2026-10-29T00:00:00Z",
        "granularity": "day",
        "limit": 1,
    }
    url = f"/v3/analytics/sites/{site['name']}"
    assert client.get(url, params=params).status_code == 401
    response = client.get(url, params=params, headers=API_HEADERS)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["total"] == 2
    assert result["items"][0]["requests"] == 20
    assert result["items"][0]["status"]["5xx"] == 2
    command = runner.invoke(
        cli.app,
        [
            "analytics",
            "site",
            site["name"],
            "--start",
            params["start"],
            "--end",
            params["end"],
            "--granularity",
            "day",
            "--limit",
            "1",
            "--json",
        ],
    )
    assert command.exit_code == 0, command.output
    assert json.loads(command.output) == result

    paths = client.get(
        f"{url}/paths",
        params={"start": params["start"], "end": params["end"]},
        headers=API_HEADERS,
    )
    assert paths.status_code == 200, paths.text
    assert paths.json()["items"][0] == {
        "path": "/index.html",
        "requests": 17,
        "bytes_sent": 170,
    }
    command = runner.invoke(
        cli.app,
        [
            "analytics",
            "paths",
            site["name"],
            "--start",
            params["start"],
            "--end",
            params["end"],
            "--json",
        ],
    )
    assert command.exit_code == 0, command.output
    assert json.loads(command.output) == paths.json()


@pytest.mark.parametrize(
    "params",
    [
        {"start": "2026-10-27T00:00:00", "end": "2026-10-28T00:00:00Z"},
        {"start": "2026-10-27T00:00:00Z", "end": "2026-10-27T00:00:00Z"},
        {
            "start": "2026-10-27T01:00:00Z",
            "end": "2026-10-28T00:00:00Z",
            "granularity": "day",
        },
        {"start": "2026-10-27T00:00:00Z"},
    ],
)
def test_invalid_api_queries_are_422(client, params):
    response = client.get(
        "/v3/analytics/sites/example-com", params=params, headers=API_HEADERS
    )
    assert response.status_code == 422, response.text


def test_unknown_site_is_404(client):
    response = client.get(
        "/v3/analytics/sites/missing/paths",
        params={"start": "2026-10-27T00:00:00Z", "end": "2026-10-28T00:00:00Z"},
        headers=API_HEADERS,
    )
    assert response.status_code == 404
