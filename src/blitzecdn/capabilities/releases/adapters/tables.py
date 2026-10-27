"""Compiled releases, the canonical state behind them, and their rendering.

Separate tables because they have different identities: a release is addressed
by what the compiler decided, its inputs by the state it read, so a compiler
bump adds a release over unchanged inputs. All content-addressed and never
updated. ``document`` is JSON fetched whole by digest.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from blitzecdn.core.persistence.tables import Base, UtcDateTime, utcnow


class ReleaseInputsRow(Base):
    """The canonical state releases were compiled from; what a rollback restores."""

    __tablename__ = "release_inputs"

    digest: Mapped[str] = mapped_column(primary_key=True)
    #: Text, not JSON: the digest is over these exact bytes.
    document: Mapped[str] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class ReleaseRow(Base):
    """One compilation: what it read, what it decided, and what it rendered."""

    __tablename__ = "releases"
    __table_args__ = (
        # The short digest every foreign key holds; unique so it is unambiguous.
        Index("ix_releases_id", "id", unique=True),
        # History is read newest-first and pruned oldest-first.
        Index("ix_releases_created_at", "created_at"),
    )

    digest: Mapped[str] = mapped_column(primary_key=True)
    id: Mapped[str] = mapped_column()
    compiler_version: Mapped[int] = mapped_column()
    inputs_digest: Mapped[str] = mapped_column(ForeignKey("release_inputs.digest"))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    #: The whole compiled release: sites, artifacts, explanations, findings.
    document: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ReleaseConfigurationRow(Base):
    """The configuration tree one release rendered to, stored so rollbacks ship it.

    Verified against its manifest and ``digest`` on read; removed with its release.
    """

    __tablename__ = "release_configurations"

    release_digest: Mapped[str] = mapped_column(
        ForeignKey("releases.digest", ondelete="CASCADE"), primary_key=True
    )
    digest: Mapped[str] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    document: Mapped[str] = mapped_column()
