"""The cache capability's own domain rules: purge entries and the hit ratio.

They were in `tests/features/sites/test_domain.py` while `cache` was a package
inside the control plane. They are not site rules — a purge entry is keyed by
the hostname nginx saw, and the hit ratio is arithmetic over what the edges
reported — so they travel with the distribution that defines them.
"""

from datetime import UTC, datetime

import pytest
from blitzecdn_cache.domain import (
    CacheStatsReport,
    EdgeStats,
    PurgeEntry,
    PurgeResult,
    SiteCacheStats,
)
from pydantic import ValidationError

from blitzecdn.core.runs import HostRun


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


def test_revalidated_counts_as_a_hit_and_expired_does_not():
    """REVALIDATED served the stored body; EXPIRED re-fetched it."""
    stats = SiteCacheStats(
        site="cdn.example.com",
        outcomes={"HIT": 6, "REVALIDATED": 2, "EXPIRED": 1, "MISS": 1},
    )
    assert stats.hits == 8
    assert stats.cacheable_requests == 10
    assert stats.hit_ratio == 0.8


def test_requests_that_never_consulted_the_cache_are_left_out_of_the_ratio():
    """A redirect-heavy site would otherwise look like a broken cache."""
    stats = SiteCacheStats(site="cdn.example.com", outcomes={"HIT": 1, "NONE": 99})
    assert stats.requests == 100
    assert stats.cacheable_requests == 1
    assert stats.hit_ratio == 1.0


def test_a_site_with_no_cacheable_traffic_has_no_hit_ratio():
    """None, not zero: an idle site must not read as a failing one."""
    assert SiteCacheStats(site="a", outcomes={"NONE": 5}).hit_ratio is None
    assert SiteCacheStats(site="a").hit_ratio is None


def test_the_fleet_hit_ratio_is_weighted_by_requests_not_by_edge():
    """Averaging per-edge ratios would let a quiet edge outvote a busy one."""
    report = CacheStatsReport(
        collected_at=datetime.now(UTC),
        edges=(
            EdgeStats(
                host="busy",
                sites=(SiteCacheStats(site="a", outcomes={"HIT": 999, "MISS": 1}),),
            ),
            EdgeStats(
                host="quiet",
                sites=(SiteCacheStats(site="a", outcomes={"MISS": 1}),),
            ),
        ),
    )
    # Mean of the two edge ratios would be ~0.50. Weighted, it is 999/1001.
    assert report.hit_ratio == 0.998


def test_a_silent_edge_is_excluded_from_the_numbers_but_still_reported():
    report = CacheStatsReport(
        collected_at=datetime.now(UTC),
        edges=(
            EdgeStats(
                host="ok", sites=(SiteCacheStats(site="a", outcomes={"HIT": 1}),)
            ),
            EdgeStats(host="down", error="unreachable"),
        ),
    )
    assert [edge.host for edge in report.reporting] == ["ok"]
    assert [edge.host for edge in report.silent] == ["down"]
    assert report.hit_ratio == 1.0


def test_by_site_sums_a_site_across_every_edge_serving_it():
    report = CacheStatsReport(
        collected_at=datetime.now(UTC),
        edges=(
            EdgeStats(
                host="edge-a",
                sites=(
                    SiteCacheStats(
                        site="a.example.com", outcomes={"HIT": 3, "MISS": 1}
                    ),
                    SiteCacheStats(site="b.example.com", outcomes={"HIT": 1}),
                ),
            ),
            EdgeStats(
                host="edge-b",
                sites=(
                    SiteCacheStats(
                        site="a.example.com", outcomes={"HIT": 1, "MISS": 3}
                    ),
                ),
            ),
        ),
    )
    merged = {site.site: site for site in report.by_site()}
    assert merged["a.example.com"].outcomes == {"HIT": 4, "MISS": 4}
    assert merged["a.example.com"].hit_ratio == 0.5
    assert merged["b.example.com"].hit_ratio == 1.0
