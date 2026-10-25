"""The fleet, as it is stored."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Column, String
from sqlalchemy.dialects.sqlite import JSON
from sqlmodel import Field

from blitzecdn.core.persistence.tables import Base, UtcDateTime, utcnow


class EdgeRow(Base, table=True):
    """The fleet, and the table the Ansible inventory plugin reads.

    A published contract: the plugin reads these columns directly, possibly under
    an interpreter without ``blitzecdn``. Its explicit SELECT is the schema, so
    change both together.
    """

    __tablename__ = "edges"
    __table_args__ = (
        CheckConstraint("length(name) > 0", name="edges_name_nonempty_check"),
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
    #: NULL means undeclared, assume the controller's set; an empty list means no
    #: optional capability at all. The inventory plugin does not read this column.
    capabilities: list[Any] | None = Field(default=None, sa_type=JSON)
    updated_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)
