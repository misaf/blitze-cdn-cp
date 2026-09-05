"""The workflow journal, as it is stored.

Part of the one description of what is on disk, and held to the rules
`core.persistence` sets out: a column per queryable fact, JSON for a value no
query reaches, and no invariant here that `domain` does not own. Alembic still
compares against `Base.metadata`, which this row registers itself in by
importing that base.

It was one of the three rows left in `core.persistence.tables` after every
capability's table moved beside the store that reads it. It stayed because the
journal was core's; it moves because the journal is this capability's.
"""

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
        # The shape of a kind, because the set of them is not this table's to
        # know: a capability names its own, and one of the three this used to
        # enumerate belongs to a distribution that need not be installed. What
        # survives is what SQL can still be sure of — non-empty, lowercase, and
        # bounded — which is the same guarantee `WorkflowKind` gives in Python.
        CheckConstraint(
            "kind <> '' AND length(kind) <= 64 AND kind = lower(kind)",
            name="workflows_kind_check",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'needs_review')",
            name="workflows_status_check",
        ),
        Index("workflows_status_idx", "status", "updated_at"),
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
