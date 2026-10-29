"""Expiry must not resurrect historical totals or forget live log progress."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import func, insert, select
from support.control_plane import build_application, with_capability_settings

from blitzecdn.capabilities.jobs.domain import JobStatus
from blitzecdn.capabilities.traffic.adapters.tables import (
    TrafficHourRow,
    TrafficRetentionRow,
)
from blitzecdn.capabilities.traffic.domain import CollectedUsage, UsageQuery
from blitzecdn.capabilities.traffic.service.collection import TrafficService
from blitzecdn.capabilities.traffic.settings import TrafficConfig
from blitzecdn.composition import Repository

NOW = datetime(2027, 2, 1, 12, 30, tzinfo=UTC)
CUTOFF = datetime(2027, 1, 2, 12, tzinfo=UTC)


@pytest.fixture
def retention_store(settings):
    repository = Repository(settings.database_path, clock=lambda: NOW)
    yield repository.traffic, repository
    repository.close()


def ingest(store, collector, directory, edge="edge-a"):
    cursors = store.checkpoints().get(edge, ())
    report = collector(
        {
            "log_directory": str(directory),
            "checkpoints": [c.model_dump() for c in cursors],
        }
    )
    return store.apply(edge, CollectedUsage.model_validate(report)), report


def rows(store):
    return store.usage(
        UsageQuery(start=CUTOFF - timedelta(days=100), end=NOW.replace(minute=0))
    )


def test_cutoff_is_hour_aligned_inclusive_and_persists_across_restarts(
    retention_store, collector, record, tmp_path, settings
):
    store, repository = retention_store
    (tmp_path / "api.example.com.access.log").write_bytes(
        record(timestamp="2027-01-02T11:59:00Z")
        + record(timestamp="2027-01-02T12:00:00Z")
    )
    ingest(store, collector, tmp_path)
    result = store.prune(30)
    assert result.cutoff == CUTOFF
    assert result.deleted_hours == 1
    assert not result.more
    assert rows(store).total == 1
    assert rows(store).items[0].hour == CUTOFF
    repository.close()
    with_restart = Repository(
        settings.database_path, clock=lambda: NOW - timedelta(days=5)
    )
    try:
        # Longer retention and clock rollback cannot reopen discarded history.
        assert with_restart.traffic.prune(90).cutoff == CUTOFF
        assert with_restart.traffic.prune(1).cutoff > CUTOFF
    finally:
        with_restart.close()


def test_missing_expired_checkpoint_can_be_forgotten_without_recounting_restored_logs(
    retention_store, collector, record, tmp_path
):
    store, _ = retention_store
    path = tmp_path / "api.example.com.access.log"
    original = record(timestamp="2026-12-01T10:00:00Z")
    path.write_bytes(original)
    ingest(store, collector, tmp_path)
    store.prune(30)
    # A stream still present remains checkpointed even though all its data expired.
    _, present = ingest(store, collector, tmp_path)
    assert present["missing_streams"] == []
    assert "edge-a" in store.checkpoints()
    path.unlink()
    _, missing = ingest(store, collector, tmp_path)
    assert len(missing["missing_streams"]) == 1
    assert store.checkpoints() == {}
    path.write_bytes(original + record(timestamp="2027-01-31T10:00:00Z"))
    result, _ = ingest(store, collector, tmp_path)
    assert result.requests == 1
    assert rows(store).total == 1
    assert rows(store).items[0].hour == datetime(2027, 1, 31, 10, tzinfo=UTC)
    assert ingest(store, collector, tmp_path)[0].requests == 0


def test_recent_missing_stream_and_out_of_order_hours_keep_checkpoint(
    retention_store, collector, record, tmp_path
):
    store, _ = retention_store
    path = tmp_path / "api.example.com.access.log"
    path.write_bytes(record(timestamp="2027-01-31T10:00:00Z"))
    ingest(store, collector, tmp_path)
    with path.open("ab") as handle:
        handle.write(record(timestamp="2026-12-01T10:00:00Z"))
    ingest(store, collector, tmp_path)
    store.prune(30)
    path.unlink()
    ingest(store, collector, tmp_path)
    assert "edge-a" in store.checkpoints()
    assert rows(store).total == 1


def test_partial_failed_and_missing_directory_inventories_never_forget_streams(
    tmp_path, collector, record
):
    missing = {"stream_id": "a" * 64, "offset": 1, "anchor": "b" * 64}
    for content in (record()[:20], b"invalid\n"):
        (tmp_path / "api.example.com.access.log").write_bytes(content)
        report = collector({"log_directory": str(tmp_path), "checkpoints": [missing]})
        assert not report.get("missing_streams")
    report = collector(
        {"log_directory": str(tmp_path / "absent"), "checkpoints": [missing]}
    )
    assert report["errors"] and not report.get("missing_streams")
    (tmp_path / "api.example.com.access.log").write_bytes(record() * 1000)
    report = collector(
        {"log_directory": str(tmp_path), "checkpoints": [missing], "max_bytes": 65536}
    )
    assert report["more"] and not report.get("missing_streams")


def test_missing_stream_report_is_bounded_and_validated(tmp_path, collector):
    checkpoints = [
        {"stream_id": f"{i:064x}", "offset": 1, "anchor": "f" * 64} for i in range(150)
    ]
    report = collector({"log_directory": str(tmp_path), "checkpoints": checkpoints})
    assert report["missing_streams"] == [c["stream_id"] for c in checkpoints[:128]]
    for changes in ({"more": True}, {"errors": ["failure"]}):
        with pytest.raises(ValidationError):
            CollectedUsage.model_validate({**report, **changes})


def test_pruning_is_bounded_and_expired_rows_are_hidden_during_catch_up(
    retention_store,
):
    store, repository = retention_store
    with repository.database.session() as session:
        session.execute(
            insert(TrafficHourRow),
            [
                {
                    "edge": "edge-a",
                    "hostname": f"h{i}.example.com",
                    "hour": CUTOFF - timedelta(hours=1),
                    "requests": 1,
                    "bytes_sent": 100,
                    "body_bytes_sent": 50,
                }
                for i in range(10_001)
            ],
        )
    first = store.prune(30)
    assert first.deleted_hours == 10_000 and first.more
    with repository.database.session() as session:
        assert session.scalar(select(func.count()).select_from(TrafficHourRow)) == 1
    assert rows(store).total == 0
    second = store.prune(30)
    assert second.deleted_hours == 1 and not second.more
    assert store.prune(30).deleted_hours == 0


def test_failed_audit_rolls_back_deletion_and_watermark(
    retention_store, collector, record, tmp_path
):
    store, repository = retention_store
    (tmp_path / "api.example.com.access.log").write_bytes(
        record(timestamp="2026-12-01T10:00:00Z")
    )
    ingest(store, collector, tmp_path)

    class BrokenAudit:
        def record(self, event):
            raise RuntimeError("audit failed")

    service = TrafficService(
        store=store, runner=SimpleNamespace(), events=BrokenAudit(), uow=repository
    )
    with pytest.raises(RuntimeError, match="audit failed"):
        service.prune("alice", retention_days=30)
    assert rows(store).total == 1
    with repository.database.session() as session:
        assert session.get(TrafficRetentionRow, 1) is None
    service.prune("alice", retention_days=0)
    assert rows(store).total == 1


def test_retention_schedule_runs_without_edges_or_collection_and_can_be_disabled(
    settings,
):
    configured = with_capability_settings(
        settings, traffic_collection_interval_seconds=0
    )
    control = build_application(settings=configured)
    try:
        control.worker.start()
        schedule = next(
            s
            for s in control.repository.schedules.list_schedules()
            if s.name == "prune-traffic"
        )
        assert schedule.interval_seconds == 3600
        job = control.job_queue.enqueue_scheduled("prune-traffic")
        control.job_runner.tick()
        assert control.repository.jobs.get(job.id).status is JobStatus.SUCCEEDED
        with control.repository.database.session() as session:
            assert session.get(TrafficRetentionRow, 1) is not None
    finally:
        control.container.close()
    disabled = build_application(
        settings=with_capability_settings(configured, traffic_retention_days=0)
    )
    try:
        disabled.worker.start()
        assert "prune-traffic" not in {
            s.name for s in disabled.repository.schedules.list_schedules()
        }
    finally:
        disabled.container.close()


@pytest.mark.parametrize("days", [-1, 3651])
def test_invalid_retention_configuration(days):
    with pytest.raises(ValidationError):
        TrafficConfig(retention_days=days)
