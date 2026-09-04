"""The fleet, as it is stored.

Part of the one description of what is on disk. The rules `core.persistence`
sets out still hold — a column per queryable fact, JSON for a value object no
query reaches, no invariants here that the domain model does not own — and
Alembic still compares against `Base.metadata`, which these rows register
themselves in by importing that base.

They sat in `core.persistence.tables` with every other capability's tables,
which meant the capability that owned the store did not own the table under
it: adding a column here was an edit to a shared module no slice owned and
every slice had to change. A table belongs beside the store that reads it, for
the same reason the wire shapes moved beside the routes that publish them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Column, String
from sqlalchemy.dialects.sqlite import JSON
from sqlmodel import Field

from blitzecdn.core.persistence.tables import Base, UtcDateTime, utcnow


class EdgeRow(Base, table=True):
    """The fleet, and the table the Ansible inventory plugin reads.

    This table is a published contract, not private storage. The plugin in
    ``ansible/plugins/inventory/`` opens the file read-only at the start of
    every run, possibly under a different interpreter with no ``blitzecdn`` on
    its path, so it reads these columns directly and has no model to validate
    against. Its explicit SELECT is the schema contract, so changes here and
    in that query must land together.
    """

    __tablename__ = "edges"
    __table_args__ = (
        CheckConstraint("port BETWEEN 1 AND 65535", name="edges_port_check"),
        CheckConstraint("length(host) > 0", name="edges_host_nonempty_check"),
        CheckConstraint("length(user) > 0", name="edges_user_nonempty_check"),
    )

    name: str = Field(sa_column=Column(String, primary_key=True))
    host: str
    user: str
    port: int
    private_key_file: str | None = None
    #: Lists rather than a child table: they are small, always read whole with
    #: the edge, and never joined or filtered on.
    public_addresses: list[Any] = Field(default_factory=list, sa_type=JSON)
    ssh_sources: list[Any] = Field(default_factory=list, sa_type=JSON)
    updated_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)
