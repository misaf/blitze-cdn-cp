"""The fleet, as it is stored."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from blitzecdn.core.persistence.tables import Base, UtcDateTime, utcnow


class EdgeRow(Base):
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

    name: Mapped[str] = mapped_column(primary_key=True)
    host: Mapped[str] = mapped_column()
    user: Mapped[str] = mapped_column()
    port: Mapped[int] = mapped_column()
    private_key_file: Mapped[str | None] = mapped_column()
    #: Lists rather than a child table: they are small, always read whole with
    #: the edge, and never joined or filtered on.
    public_addresses: Mapped[list[Any]] = mapped_column(JSON, default=list)
    ssh_sources: Mapped[list[Any]] = mapped_column(JSON, default=list)
    #: NULL means undeclared, assume the controller's set; an empty list means
    #: the edge's runtime provides none. The inventory plugin does not read this column.
    capabilities: Mapped[list[Any] | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
