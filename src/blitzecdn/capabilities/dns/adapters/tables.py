"""The zones and the records in them, as they are stored.

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

from sqlalchemy import CheckConstraint, Column, ForeignKey, String
from sqlmodel import Field

from blitzecdn.core.persistence.models import Base, UtcDateTime, utcnow


class DomainRow(Base, table=True):
    """A delegated zone. Holds no records; they are keyed by name below."""

    __tablename__ = "domains"
    __table_args__ = (
        CheckConstraint("length(name) > 0", name="domains_name_nonempty_check"),
    )

    name: str = Field(sa_column=Column(String, primary_key=True))
    updated_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)


class DnsRecordRow(Base, table=True):
    """One record: an address of its own, or a route to a site.

    There is no ``policy`` column any more and no ``proxied`` flag. Both
    existed because a record used to *be* a site; a site is its own row now, so
    what is left here is the answer DNS gives (``value``, ``ttl``) and the site
    that answers instead (``site``).

    The check constraint is the database's copy of the domain rule: exactly one
    of ``value`` and ``site``. It is written down twice deliberately — records
    also arrive from a restored backup and from a rollback's wholesale rewrite,
    neither of which goes through the record editor.
    """

    __tablename__ = "dns_records"
    __table_args__ = (
        CheckConstraint("ttl BETWEEN 1 AND 604800", name="dns_records_ttl_check"),
        CheckConstraint("type IN ('A', 'AAAA')", name="dns_records_type_check"),
        CheckConstraint("length(name) > 0", name="dns_records_name_nonempty_check"),
        CheckConstraint(
            "(value IS NULL) <> (site IS NULL)", name="dns_records_target_check"
        ),
        CheckConstraint(
            "value IS NULL OR length(value) > 0",
            name="dns_records_value_nonempty_check",
        ),
    )

    # ON DELETE CASCADE is load-bearing rather than convenience: a record
    # outliving its domain would keep deriving a virtual host for a zone we no
    # longer serve. `PRAGMA foreign_keys` is enabled per connection in
    # database.py, without which SQLite ignores this entirely.
    domain: str = Field(
        sa_column=Column(
            String, ForeignKey("domains.name", ondelete="CASCADE"), primary_key=True
        )
    )
    name: str = Field(sa_column=Column(String, primary_key=True))
    type: str = Field(sa_column=Column(String, primary_key=True))
    value: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    ttl: int
    # RESTRICT rather than CASCADE or SET NULL: deleting a site that hostnames
    # still route to is a mistake worth refusing, not one to resolve by
    # guessing. `SiteService.delete_site` names the records first; this is the
    # backstop for the paths that do not go through it.
    site: str | None = Field(
        default=None,
        sa_column=Column(
            String,
            ForeignKey("sites.name", ondelete="RESTRICT"),
            nullable=True,
            index=True,
        ),
    )
    updated_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)
