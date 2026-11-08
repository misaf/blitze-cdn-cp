"""Whether convergence keeps working: the streak an alert reads.

Deployment history answers "what happened to this run"; this answers "has it
been failing", including attempts refused before any run was recorded, such as
a release that would not compile. Pruned history cannot reset it.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ConvergenceKind(StrEnum):
    """Which convergence an outcome belongs to; a stored, closed set."""

    #: A real full-fleet deployment or rollback.
    APPLIED = "applied"
    #: A check-mode run, as the scheduled drift check makes.
    DRIFT_CHECK = "drift_check"

    @classmethod
    def of(cls, *, check: bool) -> ConvergenceKind:
        return cls.DRIFT_CHECK if check else cls.APPLIED


class ConvergenceHealth(BaseModel):
    """The last outcomes of one kind of convergence, and the current failure run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ConvergenceKind
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    #: Failures since the last success; zero once one succeeds.
    consecutive_failures: int = Field(default=0, ge=0)
