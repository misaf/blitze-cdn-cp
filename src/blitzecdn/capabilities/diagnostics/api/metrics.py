"""Prometheus text exposition with fixed, bounded labels."""

from blitzecdn.capabilities.diagnostics.domain.status import OperationalStatus
from blitzecdn.capabilities.jobs.domain import JobStatus
from blitzecdn.capabilities.workflows.domain import WorkflowStatus


def operational_metrics(report: OperationalStatus) -> str:
    lines: list[str] = []

    def gauge(name: str, help_text: str, value: int | float) -> None:
        lines.extend(
            [
                f"# HELP blitzecdn_{name} {help_text}",
                f"# TYPE blitzecdn_{name} gauge",
                f"blitzecdn_{name} {value}",
            ]
        )

    lines.extend(
        [
            "# HELP blitzecdn_jobs Retained jobs by status.",
            "# TYPE blitzecdn_jobs gauge",
            *(
                f'blitzecdn_jobs{{status="{state.value}"}} '
                f"{report.queue.counts.get(state, 0)}"
                for state in JobStatus
            ),
        ]
    )
    gauge("jobs_ready", "Pending jobs eligible to run now.", report.queue.ready)
    gauge(
        "jobs_oldest_ready_age_seconds",
        "Wait since the oldest ready job became due.",
        report.queue.oldest_ready_age_seconds,
    )
    gauge(
        "jobs_expired_leases",
        "Running jobs with expired claims.",
        report.queue.expired_leases,
    )
    lines.extend(
        [
            "# HELP blitzecdn_workers Observed workers by heartbeat state.",
            "# TYPE blitzecdn_workers gauge",
            *(
                f'blitzecdn_workers{{state="{state}"}} '
                f"{sum(worker.state == state for worker in report.workers)}"
                for state in ("idle", "busy", "stale")
            ),
        ]
    )
    gauge(
        "worker_records_unreadable",
        "Unreadable worker heartbeat records.",
        report.unreadable_worker_records,
    )
    gauge(
        "worker_heartbeat_oldest_age_seconds",
        "Age of the oldest observed heartbeat.",
        max((worker.heartbeat_age_seconds for worker in report.workers), default=0),
    )
    gauge(
        "worker_current_job_oldest_age_seconds",
        "Elapsed time of the oldest observed active job.",
        max(
            (worker.current_job_age_seconds or 0 for worker in report.workers),
            default=0,
        ),
    )
    gauge(
        "worker_last_job_duration_seconds",
        "Largest last execution duration among fresh workers.",
        max(
            (
                worker.last_duration_seconds or 0
                for worker in report.workers
                if worker.state != "stale"
            ),
            default=0,
        ),
    )
    lines.extend(
        [
            "# HELP blitzecdn_workflows Retained workflows by status, "
            "including recovery review.",
            "# TYPE blitzecdn_workflows gauge",
            *(
                f'blitzecdn_workflows{{status="{state.value}"}} '
                f"{report.workflows.counts.get(state, 0)}"
                for state in WorkflowStatus
            ),
        ]
    )
    history = report.workflows
    gauge(
        "workflow_completed_duration_seconds_average",
        "Mean elapsed duration of retained succeeded and failed workflows.",
        history.duration_seconds_sum / history.completed_count
        if history.completed_count
        else 0,
    )
    certificates = report.certificates
    gauge(
        "certificates_managed",
        "Managed certificates on this controller.",
        certificates.managed,
    )
    gauge(
        "certificates_expired",
        "Managed certificates already expired.",
        certificates.expired,
    )
    gauge(
        "certificates_expiring",
        "Managed certificates with 30 or fewer whole days remaining, "
        "including expired.",
        certificates.expiring,
    )
    gauge(
        "certificates_renewal_due",
        "ACME certificates due for renewal.",
        certificates.renewal_due,
    )
    gauge(
        "certificate_next_expiry_timestamp_seconds",
        "Earliest managed certificate expiry, or zero when none exist.",
        certificates.next_expiry_at.timestamp() if certificates.next_expiry_at else 0,
    )
    # A timestamp rather than an age, like the expiry above: the scrape is the
    # clock, so `time() - blitzecdn_backup_last_success_timestamp_seconds`
    # is the age, and a controller that never backed up reads as infinitely
    # old rather than as fresh.
    backups = report.backups
    gauge(
        "backup_last_success_timestamp_seconds",
        "Completion time of the last full backup, or zero when none is recorded.",
        backups.last_completed_at.timestamp() if backups.last_completed_at else 0,
    )
    gauge(
        "backup_last_success_size_bytes",
        "Size of the last full backup archive, or zero when none is recorded.",
        backups.last_size_bytes or 0,
    )
    return "\n".join(lines) + "\n"
