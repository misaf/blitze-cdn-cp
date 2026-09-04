"""Convergence history, and the reasons a convergence is owed.

Part of the one description of what is on disk. The rules `core.persistence`
sets out still hold — a column per queryable fact, JSON for a value object no
query reaches, no invariants here that the domain model does not own — and
Alembic still compares against `Base.metadata`, which these rows register
themselves in by importing that base.

They sat in `core.persistence.tables` with every other capability's tables,
which meant the capability that owned the store did not own the table under
it: adding a column here was an edit to a shared module no slice owned and
every slice had to change. A table belongs beside the store that reads it, for
the same reason the wire shapes moved beside the routes that publish them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Column, String
from sqlalchemy.dialects.sqlite import JSON
from sqlmodel import Field

from blitzecdn.core.persistence.tables import Base, UtcDateTime, utcnow


class DeploymentRow(Base, table=True):
    """Convergence history.

    ``result`` is the JSON of one :class:`~blitzecdn.core.domain.runs.AnsibleRun`:
    per-host counters, the tasks that changed, the tasks that failed. Raw
    stdout and stderr are deliberately not here — they are the largest thing a
    run produces and every reader would have to re-parse them to learn
    anything. They live in a log file the result names, outside the database.
    """

    __tablename__ = "deployments"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', "
            "'timed_out', 'abandoned')",
            name="deployments_status_check",
        ),
        CheckConstraint(
            "rollback_of IS NULL OR rollback_of != id",
            name="deployments_no_self_rollback_check",
        ),
    )

    id: str = Field(sa_column=Column(String, primary_key=True))
    status: str = Field(index=True)
    operator: str
    check_mode: bool
    rollback_of: str | None = Field(default=None, foreign_key="deployments.id")
    host_limit: str | None = None
    created_at: datetime = Field(
        default_factory=utcnow, sa_type=UtcDateTime, index=True
    )
    started_at: datetime | None = Field(default=None, sa_type=UtcDateTime)
    finished_at: datetime | None = Field(default=None, sa_type=UtcDateTime)
    result: dict[str, Any] | None = Field(default=None, sa_type=JSON)
    #: The desired state this deployment converged, and can roll back to.
    #: Opaque here on purpose — its shape is versioned by
    #: :mod:`blitzecdn.capabilities.deployments.domain.snapshots`, not by this schema.
    snapshot: str
    #: For a rollback: a digest of the canonical desired state as it stood when
    #: this rollback was queued. Adoption compares it against canonical state
    #: again and refuses if it moved, because ``replace_all_records`` restores
    #: wholesale and would otherwise delete a record written while the rollback
    #: was converging — silently, with nothing left to say it existed.
    #:
    #: A digest rather than a second snapshot: these rows already carry a full
    #: copy of desired state, and all this needs to answer is "the same or not".
    #: ``NULL`` on an ordinary deployment, which adopts nothing.
    canonical_digest: str | None = None


class DeploymentRequirementRow(Base, table=True):
    """Durable reasons the current desired state must reach the fleet."""

    __tablename__ = "deployment_requirements"

    kind: str = Field(sa_column=Column(String, primary_key=True))
    requested_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)
