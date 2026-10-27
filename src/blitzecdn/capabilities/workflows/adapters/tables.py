"""The workflow journal, as it is stored."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Index
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from blitzecdn.core.persistence.tables import Base, UtcDateTime, utcnow

__all__ = ["WorkflowRow"]


class WorkflowRow(Base):
    """Crash-visible progress for work that crosses out of this transaction."""

    __tablename__ = "workflows"
    __table_args__ = (
        # Kinds are named by capabilities; SQL checks only shape, as `WorkflowKind`
        # does.
        CheckConstraint(
            "kind <> '' AND length(kind) <= 64 AND kind = lower(kind)",
            name="workflows_kind_check",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'needs_review')",
            name="workflows_status_check",
        ),
        # `created_at`, not `updated_at`: every query here orders by the
        # former and none by the latter.
        Index("workflows_status_idx", "status", "created_at"),
        # The column alone, for the unfiltered list.
        Index("ix_workflows_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column()
    resource_id: Mapped[str | None] = mapped_column()
    status: Mapped[str] = mapped_column()
    operator: Mapped[str] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    #: An append-only list of checkpoints. Ordered, read whole, never queried
    #: into — a child table would buy nothing but joins.
    steps: Mapped[list[Any]] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column()
