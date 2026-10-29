from datetime import UTC, datetime, timedelta

import pytest
from support.control_plane import FakeRunner, ansible_run, build_application, host_run

from blitzecdn.capabilities.traffic.domain import CollectedUsage
from blitzecdn.capabilities.traffic.service.collection import TrafficService


def test_collector_reports_are_committed_through_the_production_service_graph(
    settings, tmp_path, collector, record
):
    path = tmp_path / "api.example.com.access.log"
    path.write_bytes(record() + record(hostname="other.example.com", bytes_sent=333))
    document = collector({"log_directory": str(tmp_path)})
    fake = FakeRunner(
        [
            ansible_run(host_run("edge-a", report=document)),
            ansible_run(host_run("edge-a", report=document)),
        ]
    )
    control = build_application(settings=settings, runner=fake)
    service = control.container.get(TrafficService)
    first = service.collect("alice")
    assert first[0].requests == 2 and first[0].bytes_sent == 453
    assert not first[0].errors
    assert service.collect("alice")[0].requests == 0
    assert fake.playbooks[-1][2]["blitzecdn_traffic_collect_checkpoints"]["edge-a"]
    hour = datetime(2026, 10, 27, 9, tzinfo=UTC)
    rows = control.repository.traffic.hourly(
        edge="edge-a",
        hostname="api.example.com",
        start=hour,
        end=hour + timedelta(hours=1),
    )
    assert rows[0].requests == 1
    events = control.repository.audit_log.list_audit_events()
    assert all(e.action == "traffic.collected" for e in events)


def test_bad_files_do_not_hide_successful_files_or_failed_edges(
    settings, tmp_path, collector, record
):
    (tmp_path / "good.test.access.log").write_bytes(record())
    (tmp_path / "bad.test.access.log").write_bytes(b"broken\n")
    document = collector({"log_directory": str(tmp_path)})
    control = build_application(
        settings=settings,
        runner=FakeRunner(
            [
                ansible_run(
                    host_run("edge-a", report=document),
                    host_run("edge-b", unreachable=1),
                )
            ]
        ),
    )
    result = control.container.get(TrafficService).collect("alice")
    assert result[0].requests == 1 and result[0].errors
    assert result[1].requests == 0 and result[1].errors
    assert set(control.repository.traffic.checkpoints()) == {"edge-a"}


def test_invalid_report_never_changes_progress(settings):
    control = build_application(
        settings=settings,
        runner=FakeRunner(
            [ansible_run(host_run("edge-a", report={"batches": [{"secret": "bad"}]}))]
        ),
    )
    result = control.container.get(TrafficService).collect("alice")
    assert result[0].errors
    assert control.repository.traffic.checkpoints() == {}


def test_audit_failure_rolls_back_the_edge_batch(settings, tmp_path, collector, record):
    (tmp_path / "api.example.com.access.log").write_bytes(record())
    document = collector({"log_directory": str(tmp_path)})
    control = build_application(
        settings=settings,
        runner=FakeRunner([ansible_run(host_run("edge-a", report=document))]),
    )
    service = control.container.get(TrafficService)

    class BrokenAudit:
        def record(self, event):
            raise RuntimeError("audit failed")

    service.events = BrokenAudit()
    with pytest.raises(RuntimeError, match="audit failed"):
        service.collect("alice")
    assert control.repository.traffic.checkpoints() == {}


def test_report_requires_at_most_one_batch_for_each_stream(tmp_path, collector, record):
    (tmp_path / "api.example.com.access.log").write_bytes(record())
    document = collector({"log_directory": str(tmp_path)})
    document["batches"] *= 2
    with pytest.raises(ValueError, match="one batch per stream"):
        CollectedUsage.model_validate(document)
