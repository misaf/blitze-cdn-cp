"""Convergence history, and the per-edge progress of each rollout."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Column, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.sqlite import JSON
from sqlmodel import Field

from blitzecdn.core.persistence.tables import Base, UtcDateTime, utcnow


class DeploymentRow(Base, table=True):
    """Convergence history; ``result`` is one serialized ``AnsibleRun``.

    Raw output stays in the log file the result names, not in the database.
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
        # Status queries order by `created_at`, so one composite index serves both.
        Index("ix_deployments_status_created_at", "status", "created_at"),
        # Indexed so SQLite's foreign-key check on bulk prune is not a scan per row.
        Index("ix_deployments_rollback_of", "rollback_of"),
        # Read by rollback selection and by release pruning.
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
    error: str | None = None
    #: The converged release, by short digest; immutable, so shared across runs.
    release_id: str = Field(foreign_key="releases.id")
    #: For a rollback: the digest of canonical inputs when it was queued. Adoption
    #: refuses if state moved since, because restoring wholesale would silently
    #: delete records written meanwhile. ``NULL`` for an ordinary deployment.
    canonical_digest: str | None = None
    #: Incremented on each takeover; target writes name it, fencing stale workers.
    generation: int = Field(sa_column=Column(Integer, nullable=False, default=0))


class DeploymentTargetRow(Base, table=True):
    """One edge's progress through one deployment.

    Rows, not JSON, because each field is queried, and a blob would make resuming
    a read-modify-write of the whole fleet that loses concurrent updates.
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
