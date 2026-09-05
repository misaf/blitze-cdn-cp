"""Durable progress for operations that cross out of a transaction.

A deployment converges a fleet and a certificate order leaves work with a CA;
neither is something SQLite can roll back, so both record checkpoints here and
a controller that restarts mid-flight turns whatever it finds unfinished into
NEEDS_REVIEW rather than silence.

This was four modules in `core`, one per layer: `domain/operations.py`,
`ports/operations.py`, `application/workflows.py` and
`persistence/workflows.py`, plus a row in core's tables, a shape in
`blitzecdn.api.models` and two routes in `deployments`. Nothing was wrong with
any of them individually — what was wrong is that a complete vertical was
filed as seven pieces of six shared modules, and `core.application` existed to
hold the one service, which is the shape `capabilities/` is for. No capability
imports this service: `deployments` and `blitzecdn-certificates` each declare
the coordinator as a Protocol of their own, and composition passes the one
object that satisfies both.
"""

from blitzecdn.capabilities.workflows.service import (
    WorkflowCoordinator,
    WorkflowProgress,
)

__all__ = ["WorkflowCoordinator", "WorkflowProgress"]
