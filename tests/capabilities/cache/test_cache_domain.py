"""The cache capability's own domain rules: purge entries and the hit ratios."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from blitzecdn.capabilities.cache.domain import (
    CacheHour,
    ConnectionsReport,
    EdgeConnections,
    OutcomeCounters,
    PurgeEntry,
    PurgeResult,
)
from blitzecdn.core.domain.runs import HostRun


def test_a_purge_uri_must_be_an_absolute_path():
    for bad in ("app.js", "", "  ", "//host/x".replace("//host", "http://host")):
        with pytest.raises(ValidationError):
            PurgeEntry(host="cdn.example.com", uri=bad)


def test_a_purge_uri_keeps_the_path_exactly_as_the_cache_keyed_it():
    """$request_uri is the key, so normalizing here would purge the wrong key."""
    entry = PurgeEntry(host="cdn.example.com", uri="  /a/./b?v=2  ")
    assert entry.uri == "/a/./b?v=2"


def test_a_purge_uri_cannot_contain_whitespace():
    with pytest.raises(ValidationError):
        PurgeEntry(host="cdn.example.com", uri="/a b")


def test_a_purge_host_is_normalized_like_every_other_hostname():
    assert PurgeEntry(host="CDN.Example.COM.", uri="/").host == "cdn.example.com"


def test_a_purge_is_incomplete_when_any_edge_failed():
    """A partial purge serves different bytes depending on which edge answers."""
    result = PurgeResult(
        purged_at=datetime.now(UTC),
        hosts=(
            HostRun(host="edge-a", changed=1),
            HostRun(host="edge-b", unreachable=1),
        ),
    )
    assert result.complete is False
    assert [host.host for host in result.succeeded] == ["edge-a"]
    assert [host.host for host in result.failed] == ["edge-b"]


def test_a_purge_that_reached_no_edge_is_not_complete():
    assert PurgeResult(purged_at=datetime.now(UTC)).complete is False


def _hour(**outcomes):
    return CacheHour(
        edge="edge-a",
        hostname="cdn.example.com",
        hour=datetime(2026, 10, 27, 9, tzinfo=UTC),
        outcomes={
            name: OutcomeCounters(requests=requests, bytes_sent=sent)
            for name, (requests, sent) in outcomes.items()
        },
    )


def test_revalidated_counts_as_a_hit_and_expired_does_not():
    """REVALIDATED served the stored body; EXPIRED re-fetched it."""
    stats = _hour(HIT=(6, 60), REVALIDATED=(2, 20), EXPIRED=(1, 10), MISS=(1, 10))
    assert stats.hits == 8
    assert stats.cacheable_requests == 10
    assert stats.hit_ratio == 0.8


def test_requests_that_never_consulted_the_cache_are_left_out_of_the_ratio():
    """A redirect-heavy site would otherwise look like a broken cache."""
    stats = _hour(HIT=(1, 10), NONE=(99, 990))
    assert stats.requests == 100
    assert stats.cacheable_requests == 1
    assert stats.hit_ratio == 1.0
    assert stats.byte_hit_ratio == 1.0
    assert stats.bytes_sent == 1000


def test_byte_hit_ratio_shows_offload_that_the_request_ratio_hides():
    """Many small hits and one large miss: most bytes still came from origin."""
    stats = _hour(HIT=(9, 900), MISS=(1, 9100))
    assert stats.hit_ratio == 0.9
    assert stats.cached_bytes_sent == 900
    assert stats.byte_hit_ratio == 0.09


def test_a_site_with_no_cacheable_traffic_has_no_hit_ratio():
    """None, not zero: an idle site must not read as a failing one."""
    assert _hour(NONE=(5, 50)).hit_ratio is None
    assert _hour(NONE=(5, 50)).byte_hit_ratio is None
    assert _hour().hit_ratio is None


def test_derived_figures_are_part_of_the_published_shape():
    document = _hour(HIT=(3, 30), MISS=(1, 10)).model_dump(mode="json")
    assert document["hit_ratio"] == 0.75
    assert document["cached_bytes_sent"] == 30
    assert document["outcomes"]["MISS"] == {"requests": 1, "bytes_sent": 10}


def test_silent_edges_are_reported_apart_from_reporting_ones():
    report = ConnectionsReport(
        collected_at=datetime.now(UTC),
        edges=(
            EdgeConnections(host="ok", connections={"active": 1}),
            EdgeConnections(host="down", error="unreachable"),
        ),
    )
    assert [edge.host for edge in report.reporting] == ["ok"]
    assert [edge.host for edge in report.silent] == ["down"]
