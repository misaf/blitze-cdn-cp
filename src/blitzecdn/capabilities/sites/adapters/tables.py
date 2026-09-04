"""The virtual hosts, as they are stored.

Part of the one description of what is on disk. The rules `core.persistence`
sets out still hold — a column per queryable fact, JSON for a value object no
query reaches, no invariants here that the domain model does not own — and
Alembic still compares against `Base.metadata`, which these rows register
themselves in by importing that base.

They sat in `core.persistence.models` with every other capability's tables,
which meant the capability that owned the store did not own the table under
it: adding a column here was an edit to a shared module no slice owned and
every slice had to change. A table belongs beside the store that reads it, for
the same reason the wire shapes moved beside the routes that publish them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Column, String
from sqlalchemy.dialects.sqlite import JSON
from sqlmodel import Field

from blitzecdn.core.persistence.models import Base, UtcDateTime, utcnow


class SiteRow(Base, table=True):
    """The virtual hosts, and the source of truth for how each one is served.

    ``policy`` stays whole because nothing queries inside it: it is written and
    read back as one document and validated by the domain model on the way out.

    ``server_names`` is the one column with a writer outside this capability. It is
    maintained by `dns` from the records routed to the site, so it is a
    projection of a *relationship* rather than of the site — which is why the
    row it lives on is canonical and this column is not.
    """

    __tablename__ = "sites"

    name: str = Field(sa_column=Column(String, primary_key=True))
    #: The virtual host names nginx answers on. A column because "which site
    #: serves this hostname" is a question worth asking of the database rather
    #: than of every decoded policy in turn.
    server_names: list[Any] = Field(default_factory=list, sa_type=JSON)
    origin_host: str
    policy: dict[str, Any] = Field(default_factory=dict, sa_type=JSON)
    updated_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)


class ProjectionStateRow(Base, table=True):
    """What revision of the source a derived table was last built from."""

    __tablename__ = "projection_state"

    name: str = Field(sa_column=Column(String, primary_key=True))
    source_revision: str
    projected_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)
