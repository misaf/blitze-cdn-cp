from datetime import UTC, datetime, timedelta

from support.control_plane import FakeRunner, ansible_run, build_application, host_run

from blitzecdn.capabilities.traffic.domain import (
    CollectedUsage,
    HourlyUsage,
    LogCursor,
    UsageBatch,
    UsageQuery,
)
from blitzecdn.composition import Repository

HOUR = datetime(2026, 10, 27, 9, tzinfo=UTC)

# ----------------------------------------------------------------------
# Stored hourly cache statistics
# ----------------------------------------------------------------------


def _usage(hostname, hour, **outcomes):
    cache = {
        name: {"requests": requests, "bytes_sent": sent}
        for name, (requests, sent) in outcomes.items()
    }
    return HourlyUsage(
        hostname=hostname,
        hour=hour,
        requests=sum(item["requests"] for item in cache.values()),
        bytes_sent=sum(item["bytes_sent"] for item in cache.values()),
        body_bytes_sent=0,
        cache=cache,
    )


def _collect(store, edge, *buckets, stream="a", start=None, offset=100):
    batch = UsageBatch(
        start=start,
        end=LogCursor(stream_id=stream * 64, offset=offset, anchor="f" * 64),
        buckets=buckets,
    )
    store.apply(edge, CollectedUsage(batches=(batch,)))
    return batch.end


def _control(settings, runner=None):
    repository = Repository(settings.database_path)
    control = build_application(
        settings=settings, repository=repository, runner=runner or FakeRunner([])
    )
    return control, repository


def _query(**fields):
    return UsageQuery(start=HOUR, end=HOUR + timedelta(hours=2), **fields)


def test_hourly_stats_come_from_stored_rows_without_contacting_edges(settings):
    fake = FakeRunner([])
    control, repository = _control(settings, fake)
    _collect(
        repository.traffic,
        "edge-a",
        _usage("cdn.example.com", HOUR, HIT=(7, 7000), MISS=(3, 300), NONE=(5, 50)),
    )

    page = control.cache.stats(_query())

    assert fake.playbooks == []
    assert page.total == 1
    (row,) = page.items
    assert (row.edge, row.hostname, row.hour) == ("edge-a", "cdn.example.com", HOUR)
    assert row.requests == 15
    assert row.cacheable_requests == 10
    assert row.hit_ratio == 0.7
    assert row.cached_bytes_sent == 7000
    assert row.byte_hit_ratio == round(7000 / 7300, 4)


def test_each_record_is_counted_once_across_retries_and_passes(settings):
    """Cache counters share traffic's cursor, so a replay adds nothing to either."""
    control, repository = _control(settings)
    first = _collect(
        repository.traffic, "edge-a", _usage("cdn.example.com", HOUR, HIT=(1, 10))
    )
    _collect(repository.traffic, "edge-a", _usage("cdn.example.com", HOUR, HIT=(1, 10)))
    _collect(
        repository.traffic,
        "edge-a",
        _usage("cdn.example.com", HOUR, HIT=(2, 20), MISS=(1, 5)),
        start=first,
        offset=200,
    )

    (row,) = control.cache.stats(_query()).items

    assert row.outcomes["HIT"].requests == 3
    assert row.outcomes["MISS"].requests == 1
    traffic = repository.traffic.usage(_query()).items[0]
    assert traffic.requests == row.requests == 4


def test_stats_filter_by_site_edge_and_hour_range(settings):
    control, repository = _control(settings)
    for edge in ("edge-a", "edge-b"):
        _collect(
            repository.traffic,
            edge,
            _usage("a.example.com", HOUR, HIT=(1, 1)),
            _usage("b.example.com", HOUR, MISS=(1, 1)),
            _usage("a.example.com", HOUR + timedelta(hours=2), HIT=(1, 1)),
        )

    every = control.cache.stats(_query())
    one_site = control.cache.stats(_query(hostname="a.example.com"))
    one_edge = control.cache.stats(_query(hostname="b.example.com", edge="edge-b"))

    assert every.total == 4
    assert [(row.hour, row.hostname, row.edge) for row in every.items] == sorted(
        (row.hour, row.hostname, row.edge) for row in every.items
    )
    assert {row.hostname for row in one_site.items} == {"a.example.com"}
    assert [(row.edge, row.hit_ratio) for row in one_edge.items] == [("edge-b", 0.0)]


def test_pruning_traffic_hours_removes_their_cache_outcomes(settings):
    _, repository = _control(settings)
    old = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(
        days=30
    )
    _collect(repository.traffic, "edge-a", _usage("a.example.com", old, HIT=(1, 1)))

    repository.traffic.prune(retention_days=1)

    with repository.database.engine.connect() as connection:
        remaining = connection.exec_driver_sql(
            "SELECT count(*) FROM traffic_cache_hours"
        ).scalar()
    assert remaining == 0


# ----------------------------------------------------------------------
# Live connection counters
# ----------------------------------------------------------------------


def _report(*, reachable=True):
    return {
        "host": "ignored",
        "collected_at": "2026-08-09T01:00:00Z",
        "nginx_reachable": reachable,
        "connections": {"active": 5, "requests": 100},
    }


def _reporting(name):
    return host_run(name, ok=5, report=_report())


def test_connections_are_read_from_every_edge(settings):
    control, _ = _control(
        settings, FakeRunner([ansible_run(_reporting("edge-a"), _reporting("edge-b"))])
    )

    report = control.cache.connections("alice")

    assert {edge.host for edge in report.reporting} == {"edge-a", "edge-b"}
    assert report.edges[0].connections == {"active": 5, "requests": 100}


def test_an_edge_that_published_nothing_is_silent_rather_than_missing(settings):
    control, _ = _control(
        settings, FakeRunner([ansible_run(_reporting("edge-a"), host_run("edge-b"))])
    )

    report = control.cache.connections("alice")

    assert [edge.host for edge in report.silent] == ["edge-b"]


def test_unreachable_and_failed_edges_say_why(settings):
    control, _ = _control(
        settings,
        FakeRunner(
            [
                ansible_run(
                    host_run("edge-a", ok=0, unreachable=1),
                    host_run("edge-b", failure="stub_status task failed"),
                )
            ]
        ),
    )

    report = control.cache.connections("alice")

    assert [(edge.host, edge.error) for edge in report.silent] == [
        ("edge-a", "unreachable"),
        ("edge-b", "the stats role failed on this edge"),
    ]


def test_a_malformed_edge_report_degrades_instead_of_raising(settings):
    """The payload crossed a machine boundary, so bad fields are dropped."""
    control, _ = _control(
        settings,
        FakeRunner(
            [
                ansible_run(
                    host_run(
                        "edge-a",
                        ok=5,
                        report={
                            "collected_at": "not a timestamp",
                            "connections": "not a mapping",
                        },
                    )
                )
            ]
        ),
    )

    (edge,) = control.cache.connections("alice").edges

    assert edge.error is None
    assert edge.collected_at is None
    assert edge.connections == {}


def test_connection_reads_are_recorded_in_the_audit_trail(settings):
    control, _ = _control(settings, FakeRunner([ansible_run(_reporting("edge-a"))]))

    control.cache.connections("alice")
    event = Repository(settings.database_path).audit_log.list_audit_events()[0]

    assert event.action == "cache.connections_collected"
    assert event.details["reporting"] == ["edge-a"]
