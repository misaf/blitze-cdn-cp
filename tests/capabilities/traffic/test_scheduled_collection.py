"""Real scheduler/queue execution and bounded catch-up policy."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from support.control_plane import (
    FakeRunner,
    ansible_run,
    build_application,
    host_run,
    seed_edge,
    with_capability_settings,
)

from blitzecdn.capabilities.jobs.domain import JobStatus
from blitzecdn.capabilities.traffic.domain import EdgeCollection
from blitzecdn.capabilities.traffic.service.collection import TrafficService
from blitzecdn.capabilities.traffic.settings import TrafficConfig
from blitzecdn.core.exceptions import ExecutionError


def test_worker_registers_the_job_and_disabled_configuration_removes_it(settings):
    control = build_application(settings=settings)
    control.worker.start()
    schedule = next(
        s
        for s in control.repository.schedules.list_schedules()
        if s.name == "collect-traffic"
    )
    assert schedule.interval_seconds == 60
    assert schedule.lease_seconds >= 3 * settings.deployment_timeout_seconds + 60
    configured = with_capability_settings(
        settings, traffic_collection_interval_seconds=0
    )
    other = build_application(settings=configured)
    other.worker.start()
    assert "collect-traffic" not in {
        s.name for s in other.repository.schedules.list_schedules()
    }


def test_scheduled_traffic_runs_through_the_durable_queue_and_persists_totals(
    settings, tmp_path, collector, record
):
    (tmp_path / "api.example.com.access.log").write_bytes(record())
    report = collector({"log_directory": str(tmp_path)})
    fake = FakeRunner([ansible_run(host_run("edge-a", report=report))])
    control = build_application(settings=settings, runner=fake)
    seed_edge(control, name="edge-a")
    control.worker.start()
    # Fire only this schedule; unrelated maintenance need not become due.
    schedule = next(
        s
        for s in control.repository.schedules.list_schedules()
        if s.name == "collect-traffic"
    )
    control.job_scheduler.tick(now=schedule.next_run_at)
    job = next(
        j
        for j in control.repository.jobs.list_jobs()
        if j.payload.get("job") == "collect-traffic"
    )
    assert control.job_queue.enqueue_scheduled("collect-traffic") is None
    control.job_runner.tick()
    assert control.repository.jobs.get(job.id).status is JobStatus.SUCCEEDED
    hour = datetime(2026, 10, 27, 9, tzinfo=UTC)
    rows = control.repository.traffic.hourly(
        edge="edge-a",
        hostname="api.example.com",
        start=hour,
        end=hour + timedelta(hours=1),
    )
    assert rows[0].requests == 1
    assert fake.playbooks[0][0] == "traffic-collect"


def test_empty_fleet_does_not_invoke_ansible_or_fail_the_job(settings):
    fake = FakeRunner()
    control = build_application(settings=settings, runner=fake)
    control.worker.start()
    job = control.job_queue.enqueue_scheduled("collect-traffic")
    control.job_runner.tick()
    assert control.repository.jobs.get(job.id).status is JobStatus.SUCCEEDED
    assert fake.playbooks == []


def test_failed_edges_fail_the_job_but_keep_healthy_edges_committed(
    settings, tmp_path, collector, record
):
    (tmp_path / "api.example.com.access.log").write_bytes(record())
    report = collector({"log_directory": str(tmp_path)})
    fake = FakeRunner(
        [
            ansible_run(
                host_run("edge-a", report=report), host_run("edge-b", unreachable=1)
            )
        ]
    )
    control = build_application(settings=settings, runner=fake)
    seed_edge(control, name="edge-a")
    control.worker.start()
    job = control.job_queue.enqueue_scheduled("collect-traffic")
    control.job_runner.tick()
    saved = control.repository.jobs.get(job.id)
    assert saved.status is JobStatus.FAILED
    assert "edge-b" in saved.last_error
    assert "edge-a" in control.repository.traffic.checkpoints()
    # Recurring work retries on a future firing, rather than an immediate loop.
    assert len(fake.playbooks) == 1
    assert control.job_queue.enqueue_scheduled("collect-traffic") is not None


def scheduled_with(results):
    calls = []

    def collect(operator, *, host_limit=None):
        calls.append(host_limit)
        return results.pop(0)

    service = SimpleNamespace(collect=collect)
    return service, calls


def test_catch_up_selects_only_successful_edges_that_are_making_progress():
    service, calls = scheduled_with(
        [
            (
                EdgeCollection(edge="edge-b", requests=2, more=True),
                EdgeCollection(edge="edge-a"),
            ),
            (EdgeCollection(edge="edge-b", requests=3, more=False),),
        ]
    )
    TrafficService.collect_scheduled(service, "scheduler", max_passes=3)
    assert calls == [None, "edge-b"]


def test_catch_up_is_bounded_even_when_logs_keep_arriving(caplog):
    service, calls = scheduled_with(
        [(EdgeCollection(edge="edge-a", requests=2, more=True),) for _ in range(3)]
    )
    TrafficService.collect_scheduled(service, "scheduler", max_passes=3)
    assert calls == [None, "edge-a", "edge-a"]
    assert "next scheduled collection" in caplog.text


def test_partial_tail_or_duplicate_delivery_does_not_cause_a_tight_loop(caplog):
    service, calls = scheduled_with([(EdgeCollection(edge="edge-a", more=True),)])
    TrafficService.collect_scheduled(service, "scheduler", max_passes=3)
    assert calls == [None]
    assert "next scheduled collection" in caplog.text


def test_failed_edges_are_not_included_in_follow_up_passes():
    service, calls = scheduled_with(
        [
            (
                EdgeCollection(edge="edge-a", errors=("bad log",)),
                EdgeCollection(edge="edge-b", requests=1, more=True),
            ),
            (EdgeCollection(edge="edge-b"),),
        ]
    )
    with pytest.raises(ExecutionError, match="edge-a"):
        TrafficService.collect_scheduled(service, "scheduler", max_passes=3)
    assert calls == [None, "edge-b"]


@pytest.mark.parametrize(
    "values",
    [
        {"collection_interval_seconds": -1},
        {"collection_max_passes": 0},
        {"collection_max_passes": 11},
    ],
)
def test_invalid_schedule_settings_are_refused(values):
    with pytest.raises(ValidationError):
        TrafficConfig(**values)


def test_large_follow_up_target_lists_wait_for_the_next_interval(caplog):
    service, calls = scheduled_with(
        [
            tuple(
                EdgeCollection(edge=f"edge-{index:04d}", requests=1, more=True)
                for index in range(100)
            )
        ]
    )
    TrafficService.collect_scheduled(service, "scheduler", max_passes=3)
    assert calls == [None]
    assert "next scheduled collection" in caplog.text
