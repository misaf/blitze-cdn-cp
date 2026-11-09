"""The fleet origin check as a queued operation: its payload and run.

The HTTP API queues it for the worker; the CLI calls the service directly.
"""

from __future__ import annotations

from typing import Any, ClassVar

from blitzecdn.capabilities.jobs import JobPayload, QueuedOperation
from blitzecdn.capabilities.origins.service.checks import OriginCheckService

__all__ = ["OriginCheckJob", "queued_operations"]


class OriginCheckJob(JobPayload):
    """Check every site's origins from the edges."""

    kind: ClassVar[str] = "origins.check"

    operator: str
    host_limit: str | None = None


def queued_operations(
    origins: OriginCheckService,
) -> tuple[QueuedOperation[Any], ...]:
    """The check's run, from its payload to its JSON report."""

    def check(job: OriginCheckJob) -> dict[str, Any]:
        report = origins.check_origins(job.operator, host_limit=job.host_limit)
        return report.model_dump(mode="json")

    return (QueuedOperation(OriginCheckJob, check),)
