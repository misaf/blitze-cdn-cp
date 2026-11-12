"""The Automatic SSL/TLS scan as a queued operation: its payload and run.

The scan probes every candidate's origin from the edges twice, then deploys
the upgrades, so the HTTP API queues it for the worker instead of holding a
request open. The CLI and the scheduler call the service directly.
"""

from __future__ import annotations

from typing import Any, ClassVar

from blitzecdn.capabilities.automatic_ssl.service.upgrades import AutomaticSslService
from blitzecdn.capabilities.jobs import JobPayload, QueuedOperation

__all__ = ["RECONCILE_DEDUPE_KEY", "AutomaticSslReconcileJob", "queued_operations"]


class AutomaticSslReconcileJob(JobPayload):
    """Probe enrolled sites' origins and apply upgrade-only SSL modes."""

    kind: ClassVar[str] = "automatic_ssl.reconcile"

    operator: str


#: One scan at a time: a second would probe the same origins and find the
#: upgrades already applied.
RECONCILE_DEDUPE_KEY = AutomaticSslReconcileJob.kind


def queued_operations(
    automatic_ssl: AutomaticSslService,
) -> tuple[QueuedOperation[Any], ...]:
    """The scan's run, from its payload to its JSON reconciliation."""

    def reconcile(job: AutomaticSslReconcileJob) -> dict[str, Any]:
        return automatic_ssl.reconcile(job.operator).model_dump(mode="json")

    return (QueuedOperation(AutomaticSslReconcileJob, reconcile),)
