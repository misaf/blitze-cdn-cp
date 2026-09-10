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

from sqlalchemy import CheckConstraint, Column, ForeignKey, Index, Integer, String
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
        # Every status query here orders by `created_at` — the queued run to
        # start next, the newest successful one to roll back to — so the two
        # belong in one index. A bare `status` index left the sort to a temp
        # b-tree, and this serves the status-only lookups just as well.
        Index("ix_deployments_status_created_at", "status", "created_at"),
        # A foreign key nothing reads, indexed for what it costs to *write*:
        # SQLite verifies no row still points at a deployment before deleting
        # it, and without this that verification is a table scan per row.
        # Pruning deletes in bulk, which made it quadratic. `dns_records.site`
        # is the same self-referential shape and has been indexed all along.
        Index("ix_deployments_rollback_of", "rollback_of"),
        # Read by rollback selection ("the newest successful run of a
        # different release") and by release pruning ("which releases is
        # something still pointing at").
        Index("ix_deployments_release_id", "release_id"),
    )

    id: str = Field(sa_column=Column(String, primary_key=True))
    status: str
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
    #: The compiled release this deployment converged, by its short digest.
    #: A foreign key rather than an embedded document: the release is immutable
    #: and content-addressed, so two deployments of unchanged state name one
    #: row instead of storing the same desired state twice.
    release_id: str = Field(foreign_key="releases.id")
    #: For a rollback: the digest of the release *inputs* as canonical state
    #: stood when this rollback was queued. Adoption compares it against canonical state
    #: again and refuses if it moved, because ``replace_all_records`` restores
    #: wholesale and would otherwise delete a record written while the rollback
    #: was converging — silently, with nothing left to say it existed.
    #:
    #: A digest rather than a second reference: all this needs to answer is
    #: "the same or not", and the state it is about is the *current* one, which
    #: no release row necessarily exists for.
    #: ``NULL`` on an ordinary deployment, which adopts nothing.
    canonical_digest: str | None = None
    #: Incremented each time a worker takes this deployment over. Every write
    #: to a target row names the generation it was read at, so a worker that
    #: was paused past its lease cannot record a result for a rollout that has
    #: since been resumed by somebody else.
    generation: int = Field(sa_column=Column(Integer, nullable=False, default=0))


class DeploymentTargetRow(Base, table=True):
    """One edge's progress through one deployment.

    Rows rather than a JSON column on the deployment, because every field here
    is queried: which edge to do next, which failed, which were never
    attempted. A blob would make "resume this rollout" a read-modify-write of
    the whole fleet's progress under a lock, which is exactly the shape that
    loses a concurrent writer's update.
    """

    __tablename__ = "deployment_targets"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'skipped')",
            name="deployment_targets_status_check",
        ),
        CheckConstraint(
            "phase IS NULL OR phase IN "
            "('prepare', 'validate', 'stage', 'activate', 'verify')",
            name="deployment_targets_phase_check",
        ),
        # The rollout's own query: this deployment's targets, in a stable order.
        Index("ix_deployment_targets_deployment_id", "deployment_id"),
    )

    deployment_id: str = Field(
        sa_column=Column(
            String, ForeignKey("deployments.id", ondelete="CASCADE"), primary_key=True
        )
    )
    edge: str = Field(sa_column=Column(String, primary_key=True))
    status: str
    phase: str | None = None
    attempts: int = Field(sa_column=Column(Integer, nullable=False, default=0))
    fence: int = Field(sa_column=Column(Integer, nullable=False, default=0))
    started_at: datetime | None = Field(default=None, sa_type=UtcDateTime)
    finished_at: datetime | None = Field(default=None, sa_type=UtcDateTime)
    last_error: str | None = None


class DeploymentRequirementRow(Base, table=True):
    """Durable reasons the current desired state must reach the fleet."""

    __tablename__ = "deployment_requirements"

    kind: str = Field(sa_column=Column(String, primary_key=True))
    requested_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)
