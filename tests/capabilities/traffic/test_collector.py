import gzip

import pytest


def collect(collector, directory, checkpoints=(), **options):
    return collector(
        {"log_directory": str(directory), "checkpoints": list(checkpoints), **options}
    )


def test_totals_are_grouped_by_actual_hostname_and_utc_hour(
    tmp_path, collector, record
):
    (tmp_path / "_wildcard.example.com.access.log").write_bytes(
        record()
        + record(hostname="other.example.com")
        + record(timestamp="2026-10-27T10:01:00+00:00")
    )
    result = collect(collector, tmp_path)
    assert not result["errors"]
    buckets = result["batches"][0]["buckets"]
    assert {
        (b["hostname"], b["hour"], b["requests"], b["bytes_sent"], b["body_bytes_sent"])
        for b in buckets
    } == {
        ("api.example.com", "2026-10-27T09:00:00+00:00", 1, 120, 100),
        ("other.example.com", "2026-10-27T09:00:00+00:00", 1, 120, 100),
        ("api.example.com", "2026-10-27T10:00:00+00:00", 1, 120, 100),
    }


def test_rename_compression_and_retries_do_not_reread_committed_records(
    tmp_path, collector, record
):
    path = tmp_path / "api.example.com.access.log"
    original = record() + record()
    path.write_bytes(original)
    batch = collect(collector, tmp_path)["batches"][0]
    cursor = batch["end"]
    path.rename(path.with_suffix(".log.1"))
    assert collect(collector, tmp_path, [cursor])["batches"] == []
    archived = path.with_suffix(".log.1")
    archived.with_suffix(".1.gz").write_bytes(gzip.compress(original))
    archived.unlink()
    assert collect(collector, tmp_path, [cursor])["batches"] == []
    path.write_bytes(record(bytes_sent=999))
    result = collect(collector, tmp_path, [cursor])
    assert not result["errors"]
    assert (
        sum(b["requests"] for batch in result["batches"] for b in batch["buckets"]) == 1
    )
    assert result["batches"][0]["end"]["stream_id"] != cursor["stream_id"]


def test_partial_tail_is_not_advanced_or_counted(tmp_path, collector, record):
    path = tmp_path / "api.example.com.access.log"
    first, second = record(), record(bytes_sent=400)
    path.write_bytes(first + second[:40])
    initial = collect(collector, tmp_path)
    assert initial["more"]
    cursor = initial["batches"][0]["end"]
    assert cursor["offset"] == len(first)
    with path.open("ab") as handle:
        handle.write(second[40:])
    batch = collect(collector, tmp_path, [cursor])["batches"][0]
    assert batch["start"] == cursor
    assert batch["buckets"][0]["requests"] == 1
    assert batch["buckets"][0]["bytes_sent"] == 400


def test_first_partial_record_creates_no_cursor(tmp_path, collector, record):
    (tmp_path / "api.example.com.access.log").write_bytes(record()[:20])
    result = collect(collector, tmp_path)
    assert result == {"batches": [], "errors": [], "more": True}


@pytest.mark.parametrize("value", [-1, True, "100", 1.5, 2**63])
def test_invalid_counters_block_the_file_without_leaking_requests(
    tmp_path, collector, record, value
):
    (tmp_path / "bad.test.access.log").write_bytes(
        record() + record(bytes_sent=value, request="private-token=secret")
    )
    (tmp_path / "good.test.access.log").write_bytes(record())
    result = collect(collector, tmp_path)
    assert len(result["batches"]) == 1
    assert result["errors"]
    assert "secret" not in str(result)


def test_truncation_and_changed_checkpoint_anchors_are_reported(
    tmp_path, collector, record
):
    path = tmp_path / "api.example.com.access.log"
    first, second = record(), record()
    path.write_bytes(first + second)
    cursor = collect(collector, tmp_path)["batches"][0]["end"]
    path.write_bytes(first)
    result = collect(collector, tmp_path, [cursor])
    assert not result["batches"] and result["errors"]
    path.write_bytes(first + second.replace(b"120", b"121"))
    result = collect(collector, tmp_path, [cursor])
    assert not result["batches"] and result["errors"]


def test_bounded_batches_eventually_read_every_record_once(tmp_path, collector, record):
    path = tmp_path / "api.example.com.access.log"
    path.write_bytes(b"".join(record() for _ in range(800)))
    checkpoints = []
    count = 0
    rounds = 0
    while True:
        result = collect(collector, tmp_path, checkpoints, max_bytes=65536)
        assert not result["errors"]
        for batch in result["batches"]:
            previous = batch["start"]["offset"] if batch["start"] else 0
            assert batch["end"]["offset"] - previous <= 65536
            count += sum(bucket["requests"] for bucket in batch["buckets"])
            checkpoints = [batch["end"]]
        rounds += 1
        assert rounds < 10
        if not result["more"]:
            break
    assert rounds > 1
    assert count == 800
    assert collect(collector, tmp_path, checkpoints)["batches"] == []


def test_duplicate_copies_are_counted_once_and_bad_gzip_is_reported(
    tmp_path, collector, record
):
    content = record()
    (tmp_path / "api.example.com.access.log").write_bytes(content)
    (tmp_path / "api.example.com.access.log.1.gz").write_bytes(gzip.compress(content))
    (tmp_path / "bad.test.access.log.2.gz").write_bytes(b"broken gzip")
    result = collect(collector, tmp_path)
    assert len(result["batches"]) == 1
    assert result["errors"]


def test_old_format_and_oversized_records_are_reported(tmp_path, collector, record):
    (tmp_path / "old.test.access.log").write_bytes(b"not-json private-token=secret\n")
    (tmp_path / "large.test.access.log").write_bytes(record(user_agent="x" * 65536))
    result = collect(collector, tmp_path)
    assert not result["batches"] and len(result["errors"]) == 2
    assert "secret" not in str(result)


def test_missing_directory_and_symlinks_are_reported(tmp_path, collector, record):
    assert collect(collector, tmp_path / "missing")["errors"]
    target = tmp_path / "target"
    target.write_bytes(record())
    (tmp_path / "api.example.com.access.log").symlink_to(target)
    assert collect(collector, tmp_path)["errors"]
