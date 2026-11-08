"""Per-site analytics: collected at the edge, summed from stored hours."""

import json
import runpy
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
from support.paths import SOURCE

from blitzecdn.capabilities.traffic.domain import (
    CollectedUsage,
    HourlyUsage,
    LogCursor,
    UsageBatch,
    UsageQuery,
)
from blitzecdn.capabilities.traffic.domain.analytics import SiteQuery
from blitzecdn.composition import Repository
from blitzecdn.entrypoints.cli import main as cli

DAY = datetime(2026, 10, 27, tzinfo=UTC)
SITE = "api.example.com"


def collect(collector, directory, **options):
    return collector({"log_directory": str(directory), "checkpoints": [], **options})


def buckets(result):
    return [bucket for batch in result["batches"] for bucket in batch["buckets"]]


def test_collector_counts_status_classes_and_busiest_paths_without_queries(
    tmp_path, collector, record
):
    (tmp_path / f"{SITE}.access.log").write_bytes(
        record(status=200, request="GET /a?token=secret HTTP/1.1")
        + record(status=204, request="GET /a#frag HTTP/1.1")
        + record(status=304, request="GET /b HTTP/2.0")
        + record(status=404, request="GET /c HTTP/2.0")
        + record(status=502, request="POST /c HTTP/2.0")
        # Neither a status class nor a path: counted as traffic only.
        + record(status=0, request="-")
        + record(request="CONNECT example.com:443 HTTP/1.1")
    )
    result = collect(collector, tmp_path, top_paths=2)
    assert not result["errors"]
    [bucket] = buckets(result)
    assert bucket["requests"] == 7
    assert bucket["status"] == {"2xx": 2, "3xx": 1, "4xx": 1, "5xx": 1}
    assert bucket["paths"] == [
        {"path": "/a", "requests": 2, "bytes_sent": 240},
        {"path": "/c", "requests": 2, "bytes_sent": 240},
    ]
    assert "secret" not in json.dumps(result)
    HourlyUsage.model_validate(bucket)


def test_paths_are_not_collected_unless_asked(tmp_path, collector, record):
    (tmp_path / f"{SITE}.access.log").write_bytes(record(request="GET /a HTTP/1.1"))
    assert buckets(collect(collector, tmp_path))[0]["paths"] == []
    for bad in (-1, 101, True, "5"):
        with pytest.raises(ValueError, match="top_paths"):
            collect(collector, tmp_path, top_paths=bad)


def test_path_tracking_is_bounded_and_keeps_the_busiest(tmp_path, collector, record):
    module = runpy.run_path(
        str(
            SOURCE
            / "capabilities/traffic/ansible/roles"
            / "blitzecdn_traffic_collect/files/collect_traffic.py"
        )
    )
    paths: dict[str, list[int]] = {}
    for _ in range(5):
        module["count_path"](paths, "/hot", 10)
    for index in range(module["PATH_TRACK"] + 1):
        module["count_path"](paths, f"/cold/{index}", 1)
    assert len(paths) <= module["PATH_TRACK"]
    assert module["top_paths"](paths, 1) == [
        {"path": "/hot", "requests": 5, "bytes_sent": 50}
    ]


def usage(hour, requests, *, status, paths, hits=0):
    cache = {"HIT": {"requests": hits, "bytes_sent": hits}} if hits else {}
    if requests - hits:
        cache["MISS"] = {"requests": requests - hits, "bytes_sent": requests - hits}
    return HourlyUsage(
        hostname=SITE,
        hour=DAY + timedelta(hours=hour),
        requests=requests,
        bytes_sent=requests,
        body_bytes_sent=requests,
        cache=cache,
        status=status,
        paths=paths,
    )


def apply(store, edge, stream, *hours, path_limit=3):
    store.apply(
        edge,
        CollectedUsage(
            batches=(
                UsageBatch(
                    end=LogCursor(stream_id=stream * 64, anchor="b" * 64, offset=1),
                    buckets=hours,
                ),
            )
        ),
        path_limit=path_limit,
    )


def seed(store):
    def paths(**counts):
        return tuple(
            {"path": f"/{name}", "requests": count, "bytes_sent": count}
            for name, count in counts.items()
        )

    apply(
        store,
        "edge-a",
        "a",
        usage(0, 4, status={"2xx": 3, "5xx": 1}, paths=paths(a=2, b=1), hits=3),
        usage(1, 2, status={"2xx": 2}, paths=paths(a=2)),
        usage(30, 1, status={"4xx": 1}, paths=paths(z=1), hits=1),
    )
    apply(
        store,
        "edge-b",
        "c",
        usage(0, 6, status={"2xx": 4, "5xx": 2}, paths=paths(b=3, c=2, d=1), hits=6),
    )
    # A later batch for the same hour: the hour keeps only its 3 busiest.
    apply(store, "edge-b", "d", usage(0, 3, status={}, paths=paths(e=3)))
    store.apply(
        "edge-c",
        CollectedUsage(
            batches=(
                UsageBatch(
                    end=LogCursor(stream_id="f" * 64, anchor="b" * 64, offset=1),
                    buckets=(usage(0, 1, status={}, paths=()),),
                ),
            )
        ),
    )
    other = usage(0, 9, status={"2xx": 9}, paths=paths(a=9))
    apply(
        store,
        "edge-a",
        "e",
        other.model_copy(update={"hostname": "other.example.com"}),
    )


@pytest.fixture
def repository(settings):
    repository = Repository(settings.database_path)
    seed(repository.traffic)
    yield repository
    repository.close()


def query(**changes):
    return SiteQuery.model_validate(
        {
            "site": SITE,
            "start": "2026-10-27T00:00:00Z",
            "end": "2026-10-29T00:00:00Z",
            **changes,
        }
    )


def test_hourly_buckets_sum_edges_and_judge_hits_and_errors(repository):
    page = repository.traffic.site_buckets(query())
    assert page.total == 3
    first, second, third = page.items
    assert first.start == DAY
    assert first.requests == 4 + 6 + 3 + 1
    assert first.status == {"2xx": 7, "5xx": 3}
    assert first.hits == 9
    assert first.cacheable_requests == 14
    assert first.hit_ratio == round(9 / 14, 4)
    assert first.error_ratio == 0.3
    assert second.start == DAY + timedelta(hours=1)
    assert (second.hits, second.hit_ratio, second.error_ratio) == (0, 0.0, 0.0)
    assert third.start == DAY + timedelta(hours=30)
    assert repository.traffic.site_buckets(query(edge="edge-b")).items[0].requests == 9
    paged = repository.traffic.site_buckets(query(limit=1, offset=1))
    assert (paged.total, [item.start for item in paged.items]) == (3, [second.start])


def test_daily_buckets_and_their_midnight_bounds(repository):
    page = repository.traffic.site_buckets(query(granularity="day"))
    assert [(item.start, item.requests) for item in page.items] == [
        (DAY, 16),
        (DAY + timedelta(days=1), 1),
    ]
    assert page.items[1].status == {"4xx": 1}
    assert page.items[1].hit_ratio == 1.0
    with pytest.raises(ValidationError, match="midnight"):
        query(granularity="day", start="2026-10-27T01:00:00Z")


def test_status_reaches_usage_rows_and_old_hours_have_none(repository):
    rows = repository.traffic.usage(
        UsageQuery.model_validate(
            {"start": "2026-10-27T00:00:00Z", "end": "2026-10-27T01:00:00Z"}
        )
    ).items
    by_edge = {row.edge: row.status for row in rows if row.hostname == SITE}
    assert by_edge == {
        "edge-a": {"2xx": 3, "5xx": 1},
        "edge-b": {"2xx": 4, "5xx": 2},
        "edge-c": {},
    }
    assert (
        repository.traffic.site_buckets(
            query(end="2026-10-27T01:00:00Z", edge="edge-c")
        )
        .items[0]
        .error_ratio
        is None
    )


def test_paths_keep_each_hours_busiest_and_sum_across_hours(repository):
    page = repository.traffic.site_paths(query())
    # edge-b's hour kept b=3, e=3, c=2 of b, c, d, e; d fell out.
    assert [(item.path, item.requests) for item in page.items] == [
        ("/a", 4),
        ("/b", 4),
        ("/e", 3),
        ("/c", 2),
        ("/z", 1),
    ]
    assert [
        item.path for item in repository.traffic.site_paths(query(edge="edge-b")).items
    ] == ["/b", "/e", "/c"]
    paged = repository.traffic.site_paths(query(limit=2, offset=2))
    assert (paged.total, [item.path for item in paged.items]) == (5, ["/e", "/c"])


def test_two_batches_of_one_hour_trim_paths_in_one_transaction(settings):
    repository = Repository(settings.database_path)

    def batch(stream, **counts):
        paths = tuple(
            {"path": f"/{name}", "requests": count, "bytes_sent": count}
            for name, count in counts.items()
        )
        total = sum(counts.values())
        return UsageBatch(
            end=LogCursor(stream_id=stream * 64, anchor="b" * 64, offset=1),
            buckets=(usage(0, total, status={"2xx": total}, paths=paths),),
        )

    repository.traffic.apply(
        "edge-a",
        CollectedUsage(
            batches=(batch("a", x=1, y=2), batch("c", x=4, z=3), batch("d", y=5))
        ),
        path_limit=2,
    )
    page = repository.traffic.site_paths(query())
    assert [(item.path, item.requests) for item in page.items] == [
        ("/x", 5),
        ("/y", 5),
    ]
    repository.close()


def test_expired_hours_leave_analytics_with_their_paths(repository, monkeypatch):
    database = repository.database
    monkeypatch.setattr(
        database, "now", lambda: DAY + timedelta(days=1, hours=1, minutes=5)
    )
    repository.traffic.prune(1)
    assert [item.start for item in repository.traffic.site_buckets(query()).items] == [
        DAY + timedelta(hours=1),
        DAY + timedelta(hours=30),
    ]
    assert [item.path for item in repository.traffic.site_paths(query()).items] == [
        "/a",
        "/z",
    ]


@pytest.fixture
def client(settings, monkeypatch):
    control = cli_control_plane(settings, monkeypatch)
    seed(control.container.get(Repository).traffic)
    serve(monkeypatch, control)
    with TestClient(control_plane_app(settings)) as client:
        yield client
    control.container.close()


@pytest.mark.parametrize(
    ("route", "command", "extra"),
    [
        (f"/v3/analytics/sites/{SITE}", "site", {"granularity": "day"}),
        (f"/v3/analytics/sites/{SITE}/paths", "paths", {"edge": "edge-b"}),
    ],
)
def test_api_and_cli_answer_alike(client, route, command, extra):
    params = {
        "start": "2026-10-27T00:00:00Z",
        "end": "2026-10-29T00:00:00Z",
        "limit": 2,
        **extra,
    }
    assert client.get(route, params=params).status_code == 401
    response = client.get(route, params=params, headers=API_HEADERS)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["total"] >= 2
    assert len(result["items"]) == 2
    options = [part for key, value in params.items() for part in (f"--{key}", value)]
    invoked = runner.invoke(
        cli.app, ["analytics", command, SITE, *map(str, options), "--json"]
    )
    assert invoked.exit_code == 0, invoked.output
    assert json.loads(invoked.output) == result


def test_api_rows_carry_ratios_and_status(client):
    response = client.get(
        f"/v3/analytics/sites/{SITE}",
        params={"start": "2026-10-27T00:00:00Z", "end": "2026-10-27T01:00:00Z"},
        headers=API_HEADERS,
    )
    [item] = response.json()["items"]
    assert item["start"] == "2026-10-27T00:00:00Z"
    assert item["status"] == {"2xx": 7, "5xx": 3}
    assert item["error_ratio"] == 0.3
    assert set(item["cache"]) == {"HIT", "MISS"}
    usage = client.get(
        "/v3/traffic/usage",
        params={"start": "2026-10-27T00:00:00Z", "end": "2026-10-27T01:00:00Z"},
        headers=API_HEADERS,
    )
    assert usage.status_code == 200, usage.text
    assert "paths" not in usage.json()["items"][0]


@pytest.mark.parametrize(
    ("site", "changes", "where"),
    [
        ("*.example.com", {}, "path"),
        (SITE, {"granularity": "day", "start": "2026-10-27T01:00:00Z"}, "query"),
        (SITE, {"granularity": "week"}, "query"),
        (SITE, {"end": "2026-10-26T00:00:00Z"}, "query"),
        (SITE, {"edge": ""}, "query"),
    ],
)
def test_invalid_analytics_queries_are_422(client, site, changes, where):
    response = client.get(
        f"/v3/analytics/sites/{site}",
        params={
            "start": "2026-10-27T00:00:00Z",
            "end": "2026-10-28T00:00:00Z",
            **changes,
        },
        headers=API_HEADERS,
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["loc"][0] == where
