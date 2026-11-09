"""Removing an edge as a queued operation: its payload and run.

The HTTP API queues it for the worker, one per edge at a time; the CLI calls
the service directly. The result says whether the host was torn down and what
each host reported.
"""

from __future__ import annotations

from typing import Any, ClassVar

from blitzecdn.capabilities.edges.service.fleet import EdgeOperationsService
from blitzecdn.capabilities.jobs import JobPayload, QueuedOperation

__all__ = ["EdgeRemovalJob", "queued_operations", "remove_dedupe_key"]


class EdgeRemovalJob(JobPayload):
    """Remove one edge from the fleet, tearing its host down if asked."""

    kind: ClassVar[str] = "edges.remove"

    operator: str
    name: str
    decommission: bool
    force: bool = False


def remove_dedupe_key(name: str) -> str:
    """One removal per edge in flight: a second would race the first's teardown."""
    return f"{EdgeRemovalJob.kind}:{name}"


def queued_operations(
    edges: EdgeOperationsService,
) -> tuple[QueuedOperation[Any], ...]:
    """The removal's run, from its payload to its JSON result."""

    def remove(job: EdgeRemovalJob) -> dict[str, Any]:
        if not job.decommission:
            edges.remove_edge(job.name, job.operator)
            return {"name": job.name, "decommissioned": False, "hosts": []}
        hosts = edges.decommission_edge(job.name, job.operator, force=job.force)
        return {
            "name": job.name,
            "decommissioned": True,
            "hosts": [host.model_dump(mode="json") for host in hosts],
        }

    return (QueuedOperation(EdgeRemovalJob, remove),)
