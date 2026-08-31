# ruff: noqa: F403,F405
from application_support import *
from blitzecdn_cache.composition import build_cache_service

# ----------------------------------------------------------------------
# Cache statistics
# ----------------------------------------------------------------------


def _stats_control(settings, *hosts):
    """A control plane whose next stats run returns these per-host results.

    Counters now arrive on the run itself, published by the role as the
    `blitzecdn_report` fact. There is no controller-side directory in the
    picture, so the roster and the numbers cannot disagree.
    """
    fake = FakeRunner([ansible_run(*hosts)])
    return ControlPlane(
        settings=settings, repository=Repository(settings.database_path), runner=fake
    ), fake  # type: ignore[arg-type]


def _report(cache, *, reachable=True):
    return {
        "host": "ignored",
        "collected_at": "2026-08-09T01:00:00Z",
        "nginx_reachable": reachable,
        "connections": {"active": 5, "requests": 100},
        "cache": cache,
    }


def _reporting(name, cache):
    return host_run(name, ok=5, report=_report(cache))


def test_statistics_are_aggregated_across_the_fleet(settings):
    control, _ = _stats_control(
        settings,
        _reporting(
            "edge-a",
            [
                {"site": "cdn.example.com", "outcome": "HIT", "requests": 7},
                {"site": "cdn.example.com", "outcome": "MISS", "requests": 3},
            ],
        ),
        _reporting(
            "edge-b", [{"site": "cdn.example.com", "outcome": "HIT", "requests": 10}]
        ),
    )

    report = build_cache_service(control).cache_stats("alice")

    assert report.hit_ratio == 0.85
    assert {edge.host for edge in report.reporting} == {"edge-a", "edge-b"}
    assert report.by_site()[0].site == "cdn.example.com"


def test_an_edge_that_published_nothing_is_silent_rather_than_missing(settings):
    """The run is the roster; a vanished edge would understate the fleet.

    An edge whose tasks ran but which published no report is a real state — the
    role failed part way, or a future role simply has nothing to say — and it
    has to read as silence rather than as absence.
    """
    control, _ = _stats_control(
        settings,
        _reporting("edge-a", [{"site": "a", "outcome": "HIT", "requests": 1}]),
        host_run("edge-b", ok=5),
    )

    report = build_cache_service(control).cache_stats("alice")

    assert [edge.host for edge in report.silent] == ["edge-b"]
    assert [edge.host for edge in report.reporting] == ["edge-a"]


def test_an_unreachable_edge_is_reported_as_unreachable(settings):
    control, _ = _stats_control(
        settings,
        _reporting("edge-a", [{"site": "a", "outcome": "HIT", "requests": 1}]),
        host_run("edge-b", ok=0, unreachable=1),
    )

    report = build_cache_service(control).cache_stats("alice")

    assert [(e.host, e.error) for e in report.silent] == [("edge-b", "unreachable")]


def test_an_edge_whose_stats_role_failed_is_not_counted(settings):
    """Its counters would be partial, and a partial number reads as a real one."""
    control, _ = _stats_control(
        settings,
        _reporting("edge-a", [{"site": "a", "outcome": "HIT", "requests": 1}]),
        host_run("edge-b", failure="collect-cache-stats.sh returned 1"),
    )

    report = build_cache_service(control).cache_stats("alice")

    assert [edge.host for edge in report.silent] == ["edge-b"]
    assert report.requests == 1


def test_a_malformed_edge_report_degrades_instead_of_raising(settings):
    """One bad payload must not take the whole fleet's numbers with it.

    The shape is ours, but it crossed a machine boundary, so every field is
    read defensively: rows that are not objects, counts that are not numbers
    and a timestamp that will not parse are dropped rather than raised on.
    """
    control, _ = _stats_control(
        settings,
        _reporting("edge-a", [{"site": "a", "outcome": "HIT", "requests": 1}]),
        host_run(
            "edge-b",
            ok=5,
            report={
                "collected_at": "not a timestamp",
                "connections": "not a mapping",
                "cache": ["not an object", {"site": "b", "requests": "many"}],
            },
        ),
    )

    report = build_cache_service(control).cache_stats("alice")

    assert report.requests == 1
    edge_b = next(edge for edge in report.edges if edge.host == "edge-b")
    assert edge_b.error is None
    assert edge_b.sites == ()
    assert edge_b.collected_at is None
    assert edge_b.connections == {}


def test_statistics_are_recorded_in_the_audit_trail(settings):
    control, _ = _stats_control(
        settings,
        _reporting("edge-a", [{"site": "a", "outcome": "HIT", "requests": 1}]),
        _reporting("edge-b", [{"site": "a", "outcome": "MISS", "requests": 1}]),
    )

    build_cache_service(control).cache_stats("alice")
    event = Repository(settings.database_path).audit_log.list_audit_events()[0]

    assert event.action == "cache.stats_collected"
    assert event.details["hit_ratio"] == 0.5


def test_a_collection_reads_only_its_own_run(settings):
    """Two overlapping collections cannot answer each other's questions.

    They used to share one controller-side directory that each emptied before
    collecting, so a run could wipe the other's reports and read the whole fleet
    as silent. Counters now arrive attached to the run that asked for them, so
    the failure has no way to occur — this pins that property rather than the
    directory bookkeeping that used to approximate it.
    """
    repository = Repository(settings.database_path)
    fake = FakeRunner(
        [
            ansible_run(
                _reporting("edge-a", [{"site": "a", "outcome": "HIT", "requests": 1}])
            ),
            ansible_run(
                _reporting("edge-a", [{"site": "a", "outcome": "HIT", "requests": 99}]),
                _reporting("edge-b", [{"site": "a", "outcome": "MISS", "requests": 5}]),
            ),
        ]
    )
    control = ControlPlane(settings=settings, repository=repository, runner=fake)  # type: ignore[arg-type]

    first = build_cache_service(control).cache_stats("alice")
    second = build_cache_service(control).cache_stats("alice")

    assert first.requests == 1
    assert [edge.host for edge in first.reporting] == ["edge-a"]
    assert second.requests == 104
    assert [edge.host for edge in second.reporting] == ["edge-a", "edge-b"]
    # Nothing on the controller's filesystem is involved any more.
    assert not (settings.state_dir / "stats").exists()
