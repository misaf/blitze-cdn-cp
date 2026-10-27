"""Convergence history, and the per-edge progress of each rollout."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from blitzecdn.core.persistence.tables import Base, UtcDateTime, utcnow


class DeploymentRow(Base):
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

    id: Mapped[str] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column()
    operator: Mapped[str] = mapped_column()
    check_mode: Mapped[bool] = mapped_column()
    rollback_of: Mapped[str | None] = mapped_column(ForeignKey("deployments.id"))
    host_limit: Mapped[str | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column()
    #: The converged release, by short digest; immutable, so shared across runs.
    release_id: Mapped[str] = mapped_column(ForeignKey("releases.id"))
    #: For a rollback: the digest of canonical inputs when it was queued. Adoption
    #: refuses if state moved since, because restoring wholesale would silently
    #: delete records written meanwhile. ``NULL`` for an ordinary deployment.
    canonical_digest: Mapped[str | None] = mapped_column()
    #: Incremented on each takeover; target writes name it, fencing stale workers.
    generation: Mapped[int] = mapped_column(default=0)


class DeploymentTargetRow(Base):
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

    deployment_id: Mapped[str] = mapped_column(
        ForeignKey("deployments.id", ondelete="CASCADE"), primary_key=True
    )
    edge: Mapped[str] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column()
    phase: Mapped[str | None] = mapped_column()
    attempts: Mapped[int] = mapped_column(default=0)
    fence: Mapped[int] = mapped_column(default=0)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    last_error: Mapped[str | None] = mapped_column()


class DeploymentRequirementRow(Base):
    """Durable reasons the current desired state must reach the fleet."""

    __tablename__ = "deployment_requirements"

    kind: Mapped[str] = mapped_column(primary_key=True)
    requested_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
