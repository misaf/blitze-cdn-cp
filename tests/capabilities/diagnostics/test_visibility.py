"""Read-only reporting, bounded metrics and operator-facing interfaces."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from support.control_plane import cli_control_plane, control_plane_app
from typer.testing import CliRunner

from blitzecdn.capabilities.diagnostics.api.metrics import operational_metrics
from blitzecdn.capabilities.diagnostics.service.status import DiagnosticsService
from blitzecdn.capabilities.jobs.adapters.monitor import WorkerMonitor
from blitzecdn.capabilities.jobs.domain.observability import WorkerHeartbeat
from blitzecdn.capabilities.tls.certificates.domain import (
    CertificateSource,
    CertificateStatus,
)
from blitzecdn.capabilities.workflows.domain import WorkflowStatus
from blitzecdn.capabilities.workflows.service.coordinator import WorkflowCoordinator
from blitzecdn.composition import Repository
from blitzecdn.entrypoints.cli.main import app


def test_status_and_metrics_show_stale_workers_expiry_and_recovery(settings, tmp_path):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    moment = [now]
    repository = Repository(settings.database_path, clock=lambda: moment[0])
    repository.workflows.create("finished", "certificate", "operator")
    moment[0] += timedelta(seconds=12)
    repository.workflows.advance("finished", WorkflowStatus.SUCCEEDED)
    repository.workflows.create("interrupted", "deployment", "operator")
    repository.workflows.advance("interrupted", WorkflowStatus.NEEDS_REVIEW)
    monitor = WorkerMonitor(tmp_path / "workers")
    monitor.directory.mkdir()
    for index, age in enumerate((0, 31)):
        worker = WorkerHeartbeat(
            worker_id=f"private-host:{index}",
            heartbeat_at=now - timedelta(seconds=age),
            current_job_id="private-job" if age else None,
            current_kind="private-kind" if age else None,
            job_started_at=now - timedelta(seconds=60) if age else None,
        )
        (monitor.directory / f"{index}.json").write_text(worker.model_dump_json())

    def certificate(days, renewable):
        return CertificateStatus(
            site="private-site",
            domains=("private.example.com",),
            source=CertificateSource.ACME if renewable else CertificateSource.UPLOADED,
            not_after=now + timedelta(days=days),
            days_remaining=days,
            expired=days <= 0,
            renewable=renewable,
            fingerprint_sha256="a" * 64,
        )

    diagnostics = DiagnosticsService(
        queue=repository.jobs,
        workflows=repository.workflows,
        workers=monitor,
        certificates=SimpleNamespace(
            certificate_statuses=lambda: [
                certificate(-1, False),
                certificate(2, False),
                certificate(10, True),
                certificate(90, True),
            ]
        ),
        clock=lambda: now,
    )
    report = diagnostics.status()
    assert [worker.state for worker in report.workers] == ["idle", "stale"]
    assert report.workers[1].current_job_age_seconds == 60
    assert report.workflows.counts[WorkflowStatus.NEEDS_REVIEW] == 1
    assert report.workflows.duration_seconds_sum == pytest.approx(12, abs=0.001)
    assert report.certificates.managed == 4
    assert report.certificates.expiring == 3
    assert report.certificates.expired == 1
    assert report.certificates.renewal_due == 1
    metrics = operational_metrics(report)
    assert 'blitzecdn_workers{state="stale"} 1' in metrics
    assert 'blitzecdn_workflows{status="needs_review"} 1' in metrics
    assert "blitzecdn_certificates_expiring 3" in metrics
    for identifier in (
        "private-host",
        "private-job",
        "private-kind",
        "private.example",
    ):
        assert identifier not in metrics
    repository.close()


def test_status_api_requires_authentication_and_reports_an_empty_controller(settings):
    with TestClient(control_plane_app(settings)) as client:
        assert client.get("/v3/status").status_code == 401
        response = client.get("/v3/status", headers={"X-API-Key": "x" * 32})
        assert response.status_code == 200
        document = response.json()
        assert document["queue"]["ready"] == 0
        assert document["workers"] == []
        assert document["certificates"]["managed"] == 0
        assert not (settings.state_dir / "workers").exists()


def test_status_cli_uses_the_same_service(settings, monkeypatch):
    control = cli_control_plane(settings, monkeypatch)
    result = CliRunner().invoke(app, ["overview", "--json"])
    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert document["queue"]["ready"] == 0
    assert document["workflows"]["completed_count"] == 0
    assert document["worker_stale_after_seconds"] == 30
    assert control.job_queue.list_jobs() == []


def test_recovery_review_is_visible_in_logs_and_persisted_state(settings, caplog):
    repository = Repository(settings.database_path)
    repository.workflows.create("interrupted", "certificate", "operator", "site-a")
    coordinator = WorkflowCoordinator(journal=repository.workflows, uow=repository)
    coordinator.reconcile_interrupted()
    (warning,) = [
        record for record in caplog.records if "recovery review" in record.message
    ]
    assert warning.workflow_id == "interrupted"
    assert warning.workflow_kind == "certificate"
    assert warning.resource_id == "site-a"
    assert repository.workflows.statistics().counts[WorkflowStatus.NEEDS_REVIEW] == 1
    repository.close()
