"""The edge-side counter blitzecdn_cache_stats runs over hostname access logs."""

import json
import runpy

import pytest
from support.paths import SOURCE

from blitzecdn.capabilities.cache import ansible

COLLECTOR = ansible.ROLES_PATH / "blitzecdn_cache_stats/files/collect_cache_stats.py"


@pytest.fixture
def module():
    return runpy.run_path(str(COLLECTOR))


def line(hostname="example.com", cache_status="HIT"):
    return json.dumps(
        {"schema_version": 1, "hostname": hostname, "cache_status": cache_status}
    )


def write(path, *lines, tail=""):
    path.write_text("".join(item + "\n" for item in lines) + tail, encoding="utf-8")


def test_counts_live_hostname_logs_by_actual_host_and_outcome(module, tmp_path):
    write(
        tmp_path / "example.com.access.log",
        line(),
        line(),
        line(cache_status="MISS"),
        line(cache_status=""),
    )
    write(
        tmp_path / "_wildcard.example.org.access.log",
        line("a.example.org", "HIT"),
        line("b.example.org", "-"),
    )
    # History and error files are not "now" and not request records.
    write(tmp_path / "example.com.access.log.1", line(cache_status="EXPIRED"))
    write(tmp_path / "example.com.error.log", "not json")

    assert module["collect"](tmp_path, 100) == [
        {"site": "a.example.org", "outcome": "HIT", "requests": 1},
        {"site": "b.example.org", "outcome": "NONE", "requests": 1},
        {"site": "example.com", "outcome": "HIT", "requests": 2},
        {"site": "example.com", "outcome": "MISS", "requests": 1},
        {"site": "example.com", "outcome": "NONE", "requests": 1},
    ]


def test_each_log_contributes_its_own_newest_window(module, tmp_path):
    """A busy site cannot crowd a quiet one out of the sample."""
    write(
        tmp_path / "busy.example.access.log",
        *[line("busy.example", "MISS")] * 5000,
        *[line("busy.example", "HIT")] * 3,
    )
    write(tmp_path / "quiet.example.access.log", line("quiet.example"))
    assert module["collect"](tmp_path, 3) == [
        {"site": "busy.example", "outcome": "HIT", "requests": 3},
        {"site": "quiet.example", "outcome": "HIT", "requests": 1},
    ]


def test_tail_skips_partial_records_at_both_ends(module, tmp_path):
    path = tmp_path / "example.com.access.log"
    records = [line(f"h{i}.example") for i in range(20000)]
    write(path, *records, tail='{"hostname":"being-writ')
    lines = module["tail"](path, 3)
    assert [json.loads(item)["hostname"] for item in lines] == [
        "h19997.example",
        "h19998.example",
        "h19999.example",
    ]
    assert module["tail"](path, 0) == []


def test_malformed_lines_are_skipped(module, tmp_path):
    write(tmp_path / "example.com.access.log", "garbage", "[1]", line())
    assert module["collect"](tmp_path, 10) == [
        {"site": "example.com", "outcome": "HIT", "requests": 1}
    ]


def test_a_fresh_edge_reports_no_requests(module, tmp_path):
    assert module["collect"](tmp_path / "missing", 10) == []
    assert module["collect"](tmp_path, 10) == []


def test_the_collector_is_standalone():
    """Ansible copies it to an edge where the control plane is not installed."""
    assert COLLECTOR.is_relative_to(SOURCE)
    source = COLLECTOR.read_text(encoding="utf-8")
    assert "import blitzecdn" not in source
    assert "from blitzecdn" not in source
