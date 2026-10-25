"""One edge's progress through a deployment, as a durable row.

Per-edge rows make partial failure explicit, let a restarted controller resume
rather than restart, and let progress be read while it happens.

Phases, in order: ``PREPARE`` compiles and publishes the release (once per
deployment, recorded per edge); ``VALIDATE`` runs check mode; ``STAGE`` renders,
tests and seals a release directory without switching to it; ``ACTIVATE``
journals the previous pointer, switches atomically and reloads, restoring on
failure; ``VERIFY`` asks the edge over the network what it serves, committing
the activation only then.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

__all__ = ["PHASE_ORDER", "DeploymentTarget", "TargetPhase", "TargetStatus"]


class TargetPhase(StrEnum):
    """How far one edge has got.

    A successful ``ACTIVATE`` only means the tasks and the reload returned zero; an
    upstream, listener, certificate or firewall can still be wrong. ``VERIFY`` is a
    request from outside the edge, and failing it fails the deployment.
    """

    PREPARE = "prepare"
    VALIDATE = "validate"
    STAGE = "stage"
    ACTIVATE = "activate"
    VERIFY = "verify"


#: The order phases are attempted in, as an explicit rule.
PHASE_ORDER: tuple[TargetPhase, ...] = (
    TargetPhase.PREPARE,
    TargetPhase.VALIDATE,
    TargetPhase.STAGE,
    TargetPhase.ACTIVATE,
    TargetPhase.VERIFY,
)


class TargetStatus(StrEnum):
    """Where one edge stands in this deployment."""

    #: Aimed at, not yet touched. The status that makes "three edges were never
    #: attempted" a fact rather than an inference.
    PENDING = "pending"
    RUNNING = "running"
    #: Every phase passed, verification included.
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    #: Never reached because an earlier edge failed; still serving what it had.
    SKIPPED = "skipped"


class DeploymentTarget(BaseModel):
    """One edge in one deployment, and how far it got."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    deployment_id: str
    edge: str
    status: TargetStatus = TargetStatus.PENDING
    #: The last phase completed, or ``None``. Resuming re-runs the interrupted
    #: phase; every phase is idempotent.
    phase: TargetPhase | None = None
    attempts: int = 0
    #: The rollout generation when this row was last written: the fence that makes
    #: a stale worker's write match nothing.
    fence: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_error: str | None = None

    @property
    def next_phase(self) -> TargetPhase | None:
        """The phase to attempt now, or ``None`` when this edge is done."""
        if self.phase is None:
            return PHASE_ORDER[0]
        index = PHASE_ORDER.index(self.phase)
        if index + 1 >= len(PHASE_ORDER):
            return None
        return PHASE_ORDER[index + 1]

    @property
    def complete(self) -> bool:
        return self.phase is TargetPhase.VERIFY
