"""Durable progress for operations that cross out of a transaction.

A deployment converges a fleet and a certificate order leaves work with a CA;
neither is something SQLite can roll back, so both record checkpoints here and
a controller that restarts mid-flight turns whatever it finds unfinished into
NEEDS_REVIEW rather than silence.

No capability imports the coordinator: `deployments` and `tls` each declare the
coordinator as a Protocol of their own, and composition passes the one object
that satisfies both.
"""

from blitzecdn.capabilities.workflows.domain import (
    Workflow,
    WorkflowKind,
    WorkflowStatus,
)
from blitzecdn.capabilities.workflows.domain.statistics import WorkflowStatistics
from blitzecdn.capabilities.workflows.ports import WorkflowHistory
from blitzecdn.capabilities.workflows.service import (
    WorkflowCoordinator,
    WorkflowProgress,
)

__all__ = [
    "Workflow",
    "WorkflowCoordinator",
    "WorkflowHistory",
    "WorkflowKind",
    "WorkflowProgress",
    "WorkflowStatistics",
    "WorkflowStatus",
]
