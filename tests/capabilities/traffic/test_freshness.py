"""Freshness describes persisted collection observations, including empty scans."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from support.cli import runner
from support.control_plane import (
    API_HEADERS,
    FakeRunner,
    ansible_run,
    build_application,
    control_plane_app,
    host_run,
    seed_edge,
    serve,
)

from blitzecdn.capabilities.traffic.domain import EdgeCollection
from blitzecdn.capabilities.traffic.service.collection import TrafficService
from blitzecdn.capabilities.traffic.settings import TrafficConfig
from blitzecdn.composition import Repository
from blitzecdn.core.exceptions import ExecutionError, NotFoundError
from blitzecdn.entrypoints.cli import common
from blitzecdn.entrypoints.cli import main as cli

NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)
EMPTY = {"batches": []}


@pytest.fixture
def tracked(settings):
    clock = [NOW]
    control = build_application(
        settings=settings, runner=FakeRunner(), clock=lambda: clock[0]
    )
    seed_edge(control, name="edge-a")
    service = control.container.get(TrafficService)
    yield control, service, clock
    control.container.close()


def report(service, document=EMPTY, **host_options):
    service.runner = SimpleNamespace(
        collect=lambda *_args, **_kw: ansible_run(
            host_run("edge-a", report=document, **host_options)
        )
    )
    return service.collect("tester")


def test_empty_collection_is_current_then_stale_at_the_threshold(tracked):
    _, service, clock = tracked
    before = service.status("edge-a")
    assert before.state == "never_collected" and before.stale
    assert before.last_success_at is None

    def collect(*_args, **_kwargs):
        assert service.status("edge-a").state == "collecting"
        clock[0] += timedelta(seconds=5)
        return ansible_run(host_run("edge-a", report=EMPTY))

    service.runner = SimpleNamespace(collect=collect)
    assert service.collect("tester")[0].requests == 0
    status = service.status("edge-a")
    assert status.state == "current" and not status.stale
    assert status.last_attempt_started_at == status.last_caught_up_at == NOW
    assert (
        status.last_attempt_finished_at
        == status.last_success_at
        == NOW + timedelta(seconds=5)
    )
    assert status.pending is False
    clock[0] = NOW + timedelta(seconds=120)
    assert service.status("edge-a").state == "stale"
    assert service.status("edge-a").stale


def test_backlog_and_failure_never_advance_last_caught_up(tracked):
    _, service, clock = tracked
    report(service)
    clock[0] += timedelta(seconds=60)
    report(service, {"batches": [], "more": True})
    pending = service.status("edge-a")
    assert pending.state == "backlogged" and pending.pending
    assert pending.last_caught_up_at == NOW
    assert pending.last_success_at == clock[0]
    clock[0] += timedelta(seconds=60)
    report(service, {"batches": [], "errors": ["secret request data"]})
    failed = service.status("edge-a")
    assert failed.state == "failed" and failed.pending is None
    assert failed.last_caught_up_at == NOW
    assert failed.last_success_at == NOW + timedelta(seconds=60)
    assert "secret" not in failed.model_dump_json()
    clock[0] += timedelta(seconds=60)
    report(service)
    recovered = service.status("edge-a")
    assert recovered.state == "current" and recovered.last_error is None
    assert recovered.last_caught_up_at == clock[0]


@pytest.mark.parametrize(
    "document,options",
    [
        ({"batches": [{"secret": "invalid"}]}, {}),
        (EMPTY, {"unreachable": 1}),
        (None, {}),
    ],
)
def test_invalid_unreachable_and_unreported_hosts_are_failed(
    tracked, document, options
):
    _, service, _ = tracked
    assert report(service, document, **options)[0].errors
    status = service.status("edge-a")
    assert status.state == "failed"
    assert status.last_success_at is None and status.last_caught_up_at is None


def test_fleet_exception_only_marks_targeted_edges(tracked):
    control, service, _ = tracked
    seed_edge(control, name="edge-b", host="192.0.2.11")

    def fail(*_args, **_kwargs):
        raise ExecutionError("private command details")

    service.runner = SimpleNamespace(collect=fail)
    with pytest.raises(ExecutionError):
        service.collect("tester", host_limit="edge-a")
    assert service.status("edge-a").state == "failed"
    assert service.status("edge-b").state == "never_collected"
    assert "private" not in service.status("edge-a").model_dump_json()


def test_missing_target_and_empty_fleet_report_are_recorded(tracked):
    control, service, _ = tracked
    seed_edge(control, name="edge-b", host="192.0.2.11")
    report(service)
    assert service.status("edge-b").state == "failed"
    service.runner = SimpleNamespace(collect=lambda *_a, **_k: ansible_run())
    with pytest.raises(ExecutionError):
        service.collect("tester")
    assert service.status("edge-a").state == "failed"


def test_stopped_worker_leaves_an_aging_attempt_and_old_success_visible(tracked):
    control, service, clock = tracked
    report(service)
    clock[0] += timedelta(seconds=60)
    control.repository.traffic.start_collection(("edge-a",), "interrupted", clock[0])
    assert service.status("edge-a").state == "collecting"
    clock[0] += timedelta(seconds=120)
    status = service.status("edge-a")
    assert status.state == "stale" and status.stale
    assert status.last_attempt_finished_at is None
    assert status.last_caught_up_at == NOW


def test_overlapping_attempts_cannot_overwrite_newer_observations(tracked):
    control, service, clock = tracked
    store = control.repository.traffic
    store.start_collection(("edge-a",), "old", NOW)
    clock[0] += timedelta(seconds=10)
    store.start_collection(("edge-a",), "new", clock[0])
    store.finish_collection(EdgeCollection(edge="edge-a", errors=("failed",)), "new")
    store.finish_collection(EdgeCollection(edge="edge-a"), "old")
    store.start_collection(("edge-a",), "delayed-start", NOW)
    assert service.status("edge-a").state == "failed"
    assert service.status("edge-a").last_attempt_started_at == clock[0]


def test_failed_audit_does_not_publish_a_successful_scan(
    tracked, tmp_path, collector, record
):
    control, service, _ = tracked
    (tmp_path / "api.example.com.access.log").write_bytes(record())
    document = collector({"log_directory": str(tmp_path)})

    class BrokenAudit:
        def record(self, event):
            raise RuntimeError("audit failed")

    service.events = BrokenAudit()
    with pytest.raises(RuntimeError, match="audit failed"):
        report(service, document)
    assert control.repository.traffic.checkpoints() == {}
    status = service.status("edge-a")
    assert status.state == "failed" and status.last_success_at is None


def test_disabled_retired_and_unknown_edges_and_restart(tracked, settings):
    control, service, _ = tracked
    with pytest.raises(NotFoundError):
        service.status("unknown")
    service.config = TrafficConfig(collection_interval_seconds=0)
    assert service.status("edge-a").state == "disabled"
    assert not service.status("edge-a").collection_enabled
    service.config = TrafficConfig(collection_interval_seconds=300)
    assert service.status("edge-a").stale_after_seconds == 600
    report(service)
    control.repository.edges.delete_edge("edge-a")
    status = service.status("edge-a")
    assert not status.registered and status.state == "disabled"
    repository = Repository(settings.database_path)
    try:
        assert repository.traffic.collection_status("edge-a").last_caught_up_at == NOW
    finally:
        repository.close()


def test_api_and_cli_match_and_status_reads_do_not_write(
    tracked, settings, monkeypatch
):
    control, service, _ = tracked
    report(service)
    serve(monkeypatch, control)
    monkeypatch.setattr(common, "container", lambda: control.container)
    before = control.repository.audit_log.list_audit_events()
    with TestClient(control_plane_app(settings)) as client:
        assert client.get("/v3/traffic/status/edge-a").status_code == 401
        response = client.get("/v3/traffic/status/edge-a", headers=API_HEADERS)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["state"] == "current"
        assert (
            client.get("/v3/traffic/status/unknown", headers=API_HEADERS).status_code
            == 404
        )
        assert (
            client.post("/v3/traffic/status/edge-a", headers=API_HEADERS).status_code
            == 405
        )
    result = runner.invoke(cli.app, ["traffic", "status", "edge-a", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == response.json()
    assert control.repository.audit_log.list_audit_events() == before
