"""Prometheus exposition with fixed, bounded labels.

Families are built per scrape from persisted state and rendered through a
throwaway registry, so ``prometheus-client`` owns escaping, number formatting
and the content type while nothing is held in process memory between scrapes.
"""

from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import datetime

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

from blitzecdn.capabilities.deployments import ConvergenceHealth
from blitzecdn.capabilities.diagnostics.domain.status import OperationalStatus
from blitzecdn.capabilities.jobs import JobStatus
from blitzecdn.capabilities.workflows import WorkflowStatus

_PREFIX = "blitzecdn_"


def _timestamp(moment: datetime | None) -> float:
    return moment.timestamp() if moment else 0


def gauge(name: str, help_text: str, value: float) -> GaugeMetricFamily:
    """One unlabelled gauge."""
    return GaugeMetricFamily(_PREFIX + name, help_text, value=value)


def labelled_gauge(
    name: str, help_text: str, label: str, samples: Iterable[tuple[str, float]]
) -> GaugeMetricFamily:
    """One gauge family with a single bounded label."""
    family = GaugeMetricFamily(_PREFIX + name, help_text, labels=[label])
    for label_value, value in samples:
        family.add_metric([label_value], value)
    return family


class _Snapshot(Collector):
    def __init__(self, families: Sequence[Metric]) -> None:
        self._families = families

    def collect(self) -> Iterator[Metric]:
        return iter(self._families)


def exposition(families: Iterable[Metric]) -> str:
    """Render families in the Prometheus text format."""
    registry = CollectorRegistry(auto_describe=False)
    registry.register(_Snapshot(tuple(families)))
    return generate_latest(registry).decode()


def operational_metrics(report: OperationalStatus) -> list[Metric]:
    history = report.workflows
    certificates = report.certificates
    backups = report.backups

    def per_kind(
        name: str, help_text: str, value: Callable[[ConvergenceHealth], float]
    ) -> GaugeMetricFamily:
        return labelled_gauge(
            name,
            help_text,
            "kind",
            ((health.kind.value, value(health)) for health in report.convergence),
        )

    return [
        labelled_gauge(
            "jobs",
            "Retained jobs by status.",
            "status",
            ((state.value, report.queue.counts.get(state, 0)) for state in JobStatus),
        ),
        gauge("jobs_ready", "Pending jobs eligible to run now.", report.queue.ready),
        gauge(
            "jobs_oldest_ready_age_seconds",
            "Wait since the oldest ready job became due.",
            report.queue.oldest_ready_age_seconds,
        ),
        gauge(
            "jobs_expired_leases",
            "Running jobs with expired claims.",
            report.queue.expired_leases,
        ),
        labelled_gauge(
            "workers",
            "Observed workers by heartbeat state.",
            "state",
            (
                (state, sum(worker.state == state for worker in report.workers))
                for state in ("idle", "busy", "stale")
            ),
        ),
        gauge(
            "worker_records_unreadable",
            "Unreadable worker heartbeat records.",
            report.unreadable_worker_records,
        ),
        gauge(
            "worker_heartbeat_oldest_age_seconds",
            "Age of the oldest observed heartbeat.",
            max((worker.heartbeat_age_seconds for worker in report.workers), default=0),
        ),
        gauge(
            "worker_current_job_oldest_age_seconds",
            "Elapsed time of the oldest observed active job.",
            max(
                (worker.current_job_age_seconds or 0 for worker in report.workers),
                default=0,
            ),
        ),
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
        ),
        labelled_gauge(
            "workflows",
            "Retained workflows by status, including recovery review.",
            "status",
            ((state.value, history.counts.get(state, 0)) for state in WorkflowStatus),
        ),
        gauge(
            "workflow_completed_duration_seconds_average",
            "Mean elapsed duration of retained succeeded and failed workflows.",
            history.duration_seconds_sum / history.completed_count
            if history.completed_count
            else 0,
        ),
        gauge(
            "certificates_managed",
            "Managed certificates on this controller.",
            certificates.managed,
        ),
        gauge(
            "certificates_expired",
            "Managed certificates already expired.",
            certificates.expired,
        ),
        gauge(
            "certificates_expiring",
            "Managed certificates with 30 or fewer whole days remaining, "
            "including expired.",
            certificates.expiring,
        ),
        gauge(
            "certificates_renewal_due",
            "ACME certificates due for renewal.",
            certificates.renewal_due,
        ),
        gauge(
            "certificate_next_expiry_timestamp_seconds",
            "Earliest managed certificate expiry, or zero when none exist.",
            _timestamp(certificates.next_expiry_at),
        ),
        gauge(
            "certificates_preflight_blocked",
            "Sites waiting for a first certificate whose last preflight blocked.",
            certificates.preflight_blocked,
        ),
        # A timestamp rather than an age, like the expiry above: the scrape is
        # the clock, so `time() - blitzecdn_backup_last_success_timestamp_seconds`
        # is the age, and a controller that never backed up reads as infinitely
        # old rather than as fresh.
        gauge(
            "backup_last_success_timestamp_seconds",
            "Completion time of the last full backup, or zero when none is recorded.",
            _timestamp(backups.last_completed_at),
        ),
        gauge(
            "backup_last_success_size_bytes",
            "Size of the last full backup archive, or zero when none is recorded.",
            backups.last_size_bytes or 0,
        ),
        # Timestamps again, and per kind: `applied` for real full-fleet runs,
        # `drift_check` for check-mode ones. A failure refused before any
        # deployment was recorded counts too, so a setting that blocks every
        # compile still raises the streak.
        per_kind(
            "convergence_last_success_timestamp_seconds",
            "Time of the last successful full-fleet run, or zero when none.",
            lambda health: _timestamp(health.last_success_at),
        ),
        per_kind(
            "convergence_last_failure_timestamp_seconds",
            "Time of the last failed full-fleet run, or zero when none.",
            lambda health: _timestamp(health.last_failure_at),
        ),
        per_kind(
            "convergence_consecutive_failures",
            "Full-fleet runs failed since the last success.",
            lambda health: health.consecutive_failures,
        ),
    ]
