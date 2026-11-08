"""Status classes and bounded top paths, counted with the traffic they ride on."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from blitzecdn.capabilities.traffic.adapters.tables import (
    TrafficPathHourRow,
    TrafficStatusHourRow,
)
from blitzecdn.capabilities.traffic.domain import (
    STORED_PATHS,
    TOP_PATHS,
    CollectedUsage,
    HourlyUsage,
    LogCursor,
    RangeQuery,
    UsageBatch,
)
from blitzecdn.composition import Repository

HOUR = datetime(2026, 10, 27, 9, tzinfo=UTC)


def collect(collector, directory, **options):
    return collector({"log_directory": str(directory), **options})


def test_status_classes_and_paths_are_counted_per_bucket(tmp_path, collector, record):
    (tmp_path / "api.example.com.access.log").write_bytes(
        record(status=200, request="GET /a?token=secret HTTP/1.1")
        + record(status=204, request="GET /a#frag HTTP/1.1")
        + record(status=503, request="GET /b HTTP/2.0")
        + record(status=404, request="GET /a HTTP/1.1")
        # Not classed and not a path, but still a request.
        + record(status=999, request="garbage")
        + record(request="GET relative HTTP/1.1")
        + record(status="200", request="GET /\x01 HTTP/1.1")
    )

    (batch,) = collect(collector, tmp_path)["batches"]
    (bucket,) = batch["buckets"]

    assert bucket["requests"] == 7
    assert bucket["status"] == {"2xx": 2, "5xx": 1, "4xx": 1}
    assert bucket["paths"] == [
        {"path": "/a", "requests": 3, "bytes_sent": 360},
        {"path": "/b", "requests": 1, "bytes_sent": 120},
    ]
    # The controller accepts exactly what the edge sends.
    HourlyUsage.model_validate(bucket)


def test_paths_are_ranked_and_bounded_at_collection(tmp_path, collector, record):
    lines = [
        record(status=200, request=f"GET /p{index} HTTP/1.1")
        for index in range(TOP_PATHS + 5)
        for _ in range(index + 1)
    ]
    (tmp_path / "api.example.com.access.log").write_bytes(b"".join(lines))

    (batch,) = collect(collector, tmp_path)["batches"]
    (bucket,) = batch["buckets"]

    assert len(bucket["paths"]) == TOP_PATHS
    assert bucket["paths"][0]["path"] == f"/p{TOP_PATHS + 4}"
    assert [item["requests"] for item in bucket["paths"]] == sorted(
        (item["requests"] for item in bucket["paths"]), reverse=True
    )


def test_tracked_paths_are_capped(tmp_path, collector, record):
    collector.__globals__["MAX_TRACKED_PATHS"] = 2
    try:
        (tmp_path / "api.example.com.access.log").write_bytes(
            record(request="GET /one HTTP/1.1")
            + record(request="GET /two HTTP/1.1")
            + record(request="GET /three HTTP/1.1")
            + record(request="GET /two HTTP/1.1")
        )
        (batch,) = collect(collector, tmp_path)["batches"]
    finally:
        collector.__globals__["MAX_TRACKED_PATHS"] = 1000
    assert [item["path"] for item in batch["buckets"][0]["paths"]] == ["/two", "/one"]


@pytest.mark.parametrize(
    "changes",
    [
        {"status": {"2xx": 2}},
        {"paths": [{"path": "/a", "requests": 2, "bytes_sent": 1}]},
        {"paths": [{"path": "/a?x=1", "requests": 1, "bytes_sent": 1}]},
        {
            "paths": [
                {"path": "/a", "requests": 1, "bytes_sent": 1},
                {"path": "/a", "requests": 1, "bytes_sent": 1},
            ]
        },
    ],
)
def test_breakdowns_cannot_exceed_or_repeat(changes):
    with pytest.raises(ValidationError):
        HourlyUsage.model_validate(
            {
                "hostname": "api.example.com",
                "hour": HOUR,
                "requests": 1,
                "bytes_sent": 10,
                "body_bytes_sent": 0,
                "cache": {"HIT": {"requests": 1, "bytes_sent": 10}},
                **changes,
            }
        )


def _bucket(hostname, hour, requests, *, status=None, paths=()):
    return HourlyUsage(
        hostname=hostname,
        hour=hour,
        requests=requests,
        bytes_sent=requests * 10,
        body_bytes_sent=0,
        cache={
            "HIT": {"requests": requests - 1, "bytes_sent": (requests - 1) * 10},
            "MISS": {"requests": 1, "bytes_sent": 10},
        },
        status=status or {"2xx": requests},
        paths=tuple(
            {"path": path, "requests": count, "bytes_sent": count * 10}
            for path, count in paths
        ),
    )


def _apply(store, edge, *buckets, stream="a", start=None, offset=100):
    batch = UsageBatch(
        start=start,
        end=LogCursor(stream_id=stream * 64, offset=offset, anchor="f" * 64),
        buckets=buckets,
    )
    store.apply(edge, CollectedUsage(batches=(batch,)))
    return batch.end


def test_status_and_paths_are_summed_per_site_hour(settings):
    repository = Repository(settings.database_path)
    store = repository.traffic
    first = _apply(
        store,
        "edge-a",
        _bucket(
            "a.example.com", HOUR, 4, status={"2xx": 3, "5xx": 1}, paths=[("/x", 3)]
        ),
        _bucket("other.example", HOUR, 9, paths=[("/x", 9)]),
    )
    # The same hour again from a later batch adds to every breakdown.
    _apply(
        store,
        "edge-a",
        _bucket("a.example.com", HOUR, 2, paths=[("/x", 1), ("/y", 1)]),
        start=first,
        offset=200,
    )
    _apply(
        store,
        "edge-b",
        _bucket("b.example.com", HOUR + timedelta(hours=1), 3, paths=[("/y", 3)]),
    )
    query = RangeQuery(
        hostnames={"a.example.com", "b.example.com"},
        start=HOUR,
        end=HOUR + timedelta(hours=3),
    )

    first_hour, second_hour = store.site_hours(query)

    assert first_hour.hour == HOUR
    assert first_hour.requests == 6
    assert first_hour.status == {"2xx": 5, "5xx": 1}
    assert first_hour.cache["HIT"].requests == 4
    assert second_hour.hour == HOUR + timedelta(hours=1)
    assert second_hour.status == {"2xx": 3}
    paths = store.top_paths(query, limit=10, offset=0)
    assert paths.total == 2
    # Ties rank by path.
    assert [(item.path, item.requests) for item in paths.items] == [
        ("/x", 4),
        ("/y", 4),
    ]
    assert store.top_paths(query, limit=1, offset=1).total == 2
    assert len(store.top_paths(query, limit=1, offset=1).items) == 1
    assert store.site_hours(query.model_copy(update={"hostnames": frozenset()})) == ()
    assert store.hostnames(start=HOUR, end=HOUR + timedelta(hours=1)) == {
        "a.example.com",
        "other.example",
    }
    repository.close()


def test_stored_paths_are_trimmed_to_the_best_ranked(settings):
    repository = Repository(settings.database_path)
    store = repository.traffic
    cursor = None
    for batch in range(3):
        paths = [
            (f"/b{batch}-{index}", 100 - batch * TOP_PATHS - index)
            for index in range(TOP_PATHS)
        ]
        cursor = _apply(
            store,
            "edge-a",
            _bucket("a.example.com", HOUR, 1000, paths=paths),
            start=cursor,
            offset=100 * (batch + 1),
        )
    query = RangeQuery(
        hostnames={"a.example.com"}, start=HOUR, end=HOUR + timedelta(hours=1)
    )
    page = store.top_paths(query, limit=500, offset=0)
    assert page.total == STORED_PATHS
    assert {item.path for item in page.items} == {
        f"/b{batch}-{index}" for batch in range(2) for index in range(TOP_PATHS)
    }
    repository.close()


def test_breakdowns_are_pruned_and_hidden_with_their_hour(settings):
    repository = Repository(settings.database_path)
    store = repository.traffic
    old = datetime(2020, 1, 1, tzinfo=UTC)
    _apply(store, "edge-a", _bucket("a.example.com", old, 2, paths=[("/x", 2)]))
    query = RangeQuery(
        hostnames={"a.example.com"}, start=old, end=old + timedelta(hours=1)
    )
    assert store.site_hours(query)
    store.prune(1)
    assert store.site_hours(query) == ()
    assert store.top_paths(query, limit=10, offset=0).total == 0
    assert store.hostnames(start=old, end=old + timedelta(hours=1)) == frozenset()
    with repository.database.session() as session:
        for table in (TrafficStatusHourRow, TrafficPathHourRow):
            assert session.scalar(select(func.count()).select_from(table)) == 0
    repository.close()
