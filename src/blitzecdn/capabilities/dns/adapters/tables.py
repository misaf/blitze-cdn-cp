"""The zones, their policy, the rules that bend it, and their records, stored.

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

from sqlalchemy import CheckConstraint, Column, ForeignKey, String
from sqlalchemy.dialects.sqlite import JSON
from sqlmodel import Field

from blitzecdn.core.persistence.tables import Base, UtcDateTime, utcnow


class DomainRow(Base, table=True):
    """A delegated zone and its policy. Holds no records; they are keyed below.

    ``policy`` stays whole because nothing queries inside it: it is written and
    read back as one document and validated by the domain model on the way out.
    Each record's address is the origin the edge fetches from, so there is
    nothing zone-level to ask "which zones proxy to this origin" about — every
    zone that proxies anywhere can be answered from the records table.
    """

    __tablename__ = "domains"
    __table_args__ = (
        CheckConstraint("length(name) > 0", name="domains_name_nonempty_check"),
    )

    name: str = Field(sa_column=Column(String, primary_key=True))
    policy: dict[str, Any] = Field(default_factory=dict, sa_type=JSON)
    updated_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)


class DnsRecordRow(Base, table=True):
    """One record: an address, and whether the edge serves the hostname.

    No policy column and no site reference. The policy is the zone's, and which
    virtual host serves this hostname is computed from the zone, its rules and
    the record's own address rather than stored — so what is left here is the
    address (``value``, ``ttl``) and whether the edge answers instead
    (``proxied``). ``value`` is the DNS answer when ``proxied`` is false and
    the origin the edge fetches from when it is true.

    The check constraints are the database's copy of the domain rules: a value
    is always present, and it carries an address that matches its type. They
    are written down twice deliberately — records also arrive from a restored
    backup and from a rollback's wholesale rewrite, neither of which goes
    through the editor.
    """

    __tablename__ = "dns_records"
    __table_args__ = (
        CheckConstraint("ttl BETWEEN 1 AND 604800", name="dns_records_ttl_check"),
        CheckConstraint("type IN ('A', 'AAAA')", name="dns_records_type_check"),
        CheckConstraint("length(name) > 0", name="dns_records_name_nonempty_check"),
        CheckConstraint(
            "value IS NOT NULL AND length(value) > 0",
            name="dns_records_value_check",
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
    value: str = Field(sa_column=Column(String, nullable=False))
    ttl: int
    proxied: bool = True
    updated_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)


class RuleRow(Base, table=True):
    """One override in a zone.

    ``overrides`` stays whole because nothing queries inside it: it is written
    and read back as one document and validated by the domain model on the way
    out. ``match`` and ``priority`` are columns because ordering the rules of a
    zone is the one thing every read of this table does.
    """

    __tablename__ = "zone_rules"
    __table_args__ = (
        CheckConstraint("length(name) > 0", name="zone_rules_name_nonempty_check"),
        CheckConstraint("length(match) > 0", name="zone_rules_match_nonempty_check"),
        CheckConstraint(
            "priority BETWEEN 1 AND 1000", name="zone_rules_priority_check"
        ),
    )

    # ON DELETE CASCADE for the same reason the records have it: a rule
    # outliving its zone would be an override for a domain we no longer serve.
    domain: str = Field(
        sa_column=Column(
            String, ForeignKey("domains.name", ondelete="CASCADE"), primary_key=True
        )
    )
    name: str = Field(sa_column=Column(String, primary_key=True))
    priority: int
    match: str
    overrides: dict[str, Any] = Field(default_factory=dict, sa_type=JSON)
    enabled: bool = True
    updated_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)
