"""Durable progress for operations that cross out of a transaction.

A deployment converges a fleet and a certificate order leaves work with a CA;
neither is something SQLite can roll back, so both record checkpoints here and
a controller that restarts mid-flight turns whatever it finds unfinished into
NEEDS_REVIEW rather than silence.

No capability imports this service: `deployments` and `tls` each declare the
coordinator as a Protocol of their own, and composition passes the one object
that satisfies both.
"""

from blitzecdn.capabilities.workflows.service import (
    WorkflowCoordinator,
    WorkflowProgress,
)

__all__ = ["WorkflowCoordinator", "WorkflowProgress"]
