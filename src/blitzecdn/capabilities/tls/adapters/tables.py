"""Blocked certificate preflights, kept to back off unpointed sites."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column

from blitzecdn.core.persistence.tables import Base, UtcDateTime

__all__ = ["PreflightAttemptRow"]


class PreflightAttemptRow(Base):
    __tablename__ = "certificate_preflight_attempts"
    __table_args__ = (
        CheckConstraint(
            "failures >= 1", name="certificate_preflight_attempts_failures_check"
        ),
    )

    site: Mapped[str] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column()
    failures: Mapped[int] = mapped_column()
    checked_at: Mapped[datetime] = mapped_column(UtcDateTime)
    next_check_at: Mapped[datetime] = mapped_column(UtcDateTime)
    summary: Mapped[str] = mapped_column()
