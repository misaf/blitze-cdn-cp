"""The workflow journal, as it is stored."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Column, Index, String
from sqlalchemy.dialects.sqlite import JSON
from sqlmodel import Field

from blitzecdn.core.persistence.tables import Base, UtcDateTime, utcnow

__all__ = ["WorkflowRow"]


class WorkflowRow(Base, table=True):
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

    id: str = Field(sa_column=Column(String, primary_key=True))
    kind: str
    resource_id: str | None = None
    status: str
    operator: str
    created_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)
    updated_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)
    #: An append-only list of checkpoints. Ordered, read whole, never queried
    #: into — a child table would buy nothing but joins.
    steps: list[Any] = Field(default_factory=list, sa_type=JSON)
    error: str | None = None
