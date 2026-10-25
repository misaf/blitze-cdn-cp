"""Where compiled releases, the state behind them and their rendering are kept.

Three tables, because the three things have different lifetimes and different
identities. A release is addressed by the digest of what the compiler decided;
its inputs are addressed by the digest of the canonical state it read. Bumping
the compiler version produces a second release over one unchanged set of
inputs, and storing the inputs on the release row would store that state twice
— once per compilation — for no reader's benefit.

Both are content-addressed, so both are immutable by construction. Nothing
updates a row here: a compilation either finds its digest already present, in
which case the stored row is by definition identical, or inserts one. That is
what makes a release safe to converge twice and safe to roll back to.

The rules `core.persistence.tables` sets out still hold. ``document`` is JSON
because nothing queries into it — a release is fetched whole, by digest — and
the shape inside it is versioned by the domain model rather than by this
schema.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Column, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.sqlite import JSON
from sqlmodel import Field

from blitzecdn.core.persistence.tables import Base, UtcDateTime, utcnow


class ReleaseInputsRow(Base, table=True):
    """The canonical state one or more releases were compiled from.

    Kept beyond the release that first stored it, because a rollback restores
    *these* — the zones, rules and records — and not the artifact. Converging
    an old artifact would put an edge back where it was and leave the control
    plane still asserting the newer state; adopting the inputs is what makes a
    rollback a change of desired state rather than a temporary override.
    """

    __tablename__ = "release_inputs"

    digest: str = Field(sa_column=Column(String, primary_key=True))
    #: The encoded document, exactly as digested. Text rather than JSON: the
    #: digest is over these bytes, and a JSON column that re-serialised them on
    #: the way out could return a document whose digest is no longer its key.
    document: str
    created_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)


class ReleaseRow(Base, table=True):
    """One compilation: what it read, what it decided, and what it rendered."""

    __tablename__ = "releases"
    __table_args__ = (
        # `id` is the short digest an operator types and every foreign key
        # holds. Unique because two distinct releases sharing a short form
        # would make a deployment ambiguous about which it converged.
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
    """The edge configuration tree one release rendered to, as it was rendered.

    Stored rather than re-rendered when a deployment needs it, so a rollback
    ships the exact bytes the release first produced even after the templates
    that produced them have changed. Removed with its release. ``document`` is
    the encoded bundle, verified against its manifest and ``digest`` on read.
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
