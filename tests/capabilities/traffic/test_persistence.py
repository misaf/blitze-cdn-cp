from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from blitzecdn.capabilities.traffic.domain import (
    CollectedUsage,
    HourlyUsage,
    LogCursor,
    UsageBatch,
)
from blitzecdn.composition import Repository
from blitzecdn.core.exceptions import ConflictError

HOUR = datetime(2026, 10, 27, 9, tzinfo=UTC)


def batch(stream="a", start=None, offset=100, sent=120):
    return UsageBatch(
        start=start,
        end=LogCursor(stream_id=stream * 64, offset=offset, anchor="f" * 64),
        buckets=(
            HourlyUsage(
                hostname="api.example.com",
                hour=HOUR,
                requests=1,
                bytes_sent=sent,
                body_bytes_sent=100,
            ),
        ),
    )


def totals(store, edge="edge-a"):
    return store.hourly(
        edge=edge, hostname="api.example.com", start=HOUR, end=HOUR + timedelta(hours=1)
    )


def test_retries_restarts_and_append_only_progress_count_once(settings):
    repository = Repository(settings.database_path)
    store = repository.traffic
    first = batch()
    report = CollectedUsage(batches=(first,))
    assert store.apply("edge-a", report).requests == 1
    assert store.apply("edge-a", report).requests == 0
    second = batch(start=first.end, offset=200)
    store.apply("edge-a", CollectedUsage(batches=(second,)))
    assert store.apply("edge-a", report).requests == 0
    repository.close()
    reopened = Repository(settings.database_path)
    assert totals(reopened.traffic)[0].requests == 2
    assert reopened.traffic.checkpoints()["edge-a"] == (second.end,)
    reopened.close()


def test_edges_and_stream_generations_remain_separate(settings):
    store = Repository(settings.database_path).traffic
    store.apply("edge-a", CollectedUsage(batches=(batch("a"), batch("b"))))
    store.apply("edge-b", CollectedUsage(batches=(batch("a"),)))
    assert totals(store)[0].requests == 2
    assert totals(store, "edge-b")[0].requests == 1


def test_stale_overlapping_batches_are_rejected_without_partial_commits(settings):
    store = Repository(settings.database_path).traffic
    first = batch()
    store.apply("edge-a", CollectedUsage(batches=(first,)))
    with pytest.raises(ConflictError, match="progress changed"):
        store.apply(
            "edge-a",
            CollectedUsage(batches=(batch("b"), batch(start=None, offset=200))),
        )
    assert totals(store)[0].requests == 1
    assert store.checkpoints()["edge-a"] == (first.end,)


def test_counter_overflow_rolls_back_cursor_and_all_increments(settings):
    store = Repository(settings.database_path).traffic
    first = batch(sent=2**63 - 1)
    store.apply("edge-a", CollectedUsage(batches=(first,)))
    with pytest.raises(ConflictError, match="integer range"):
        store.apply("edge-a", CollectedUsage(batches=(batch("b"),)))
    assert totals(store)[0].bytes_sent == 2**63 - 1
    assert store.checkpoints()["edge-a"] == (first.end,)


def test_outer_failure_rolls_back_totals_and_checkpoint(settings):
    repository = Repository(settings.database_path)
    with pytest.raises(RuntimeError), repository.transaction():
        repository.traffic.apply("edge-a", CollectedUsage(batches=(batch(),)))
        raise RuntimeError("audit failed")
    assert totals(repository.traffic) == ()
    assert repository.traffic.checkpoints() == {}


def test_independent_database_connections_serialize_duplicate_collections(settings):
    one, two = Repository(settings.database_path), Repository(settings.database_path)
    report = CollectedUsage(batches=(batch(),))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(store.apply, "edge-a", report)
            for store in (one.traffic, two.traffic)
        ]
        assert sorted(f.result().requests for f in futures) == [0, 1]
    assert totals(one.traffic)[0].requests == 1
    one.close()
    two.close()
