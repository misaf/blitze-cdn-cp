"""Compiled releases, the canonical state behind them, and their rendering.

Separate tables because they have different identities: a release is addressed
by what the compiler decided, its inputs by the state it read, so a compiler
bump adds a release over unchanged inputs. All content-addressed and never
updated. ``document`` is JSON fetched whole by digest.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Column, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.sqlite import JSON
from sqlmodel import Field

from blitzecdn.core.persistence.tables import Base, UtcDateTime, utcnow


class ReleaseInputsRow(Base, table=True):
    """The canonical state releases were compiled from; what a rollback restores."""

    __tablename__ = "release_inputs"

    digest: str = Field(sa_column=Column(String, primary_key=True))
    #: Text, not JSON: the digest is over these exact bytes.
    document: str
    created_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)


class ReleaseRow(Base, table=True):
    """One compilation: what it read, what it decided, and what it rendered."""

    __tablename__ = "releases"
    __table_args__ = (
        # The short digest every foreign key holds; unique so it is unambiguous.
        Index("ix_releases_id", "id", unique=True),
        # History is read newest-first and pruned oldest-first.
        Index("ix_releases_created_at", "created_at"),
    )

    digest: str = Field(sa_column=Column(String, primary_key=True))
    id: str
    compiler_version: int = Field(sa_column=Column(Integer, nullable=False))
    inputs_digest: str = Field(foreign_key="release_inputs.digest")
    created_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)
    #: The whole compiled release: sites, artifacts, explanations, findings.
    document: dict[str, Any] = Field(default_factory=dict, sa_type=JSON)


class ReleaseConfigurationRow(Base, table=True):
    """The configuration tree one release rendered to, stored so rollbacks ship it.

    Verified against its manifest and ``digest`` on read; removed with its release.
    """

    __tablename__ = "release_configurations"

    release_digest: str = Field(
        sa_column=Column(
            String, ForeignKey("releases.digest", ondelete="CASCADE"), primary_key=True
        )
    )
    digest: str = Field(sa_column=Column(String, nullable=False))
    created_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)
    document: str
