"""The base every table is declared on, and core's own two.

Named for what it holds. It was `models.py`, which was the third meaning of
that word in one distribution — a capability's `domain` models hold the
invariants, an `api` module holds the wire shapes, and this holds the physical
schema — and the only one of the three that could not be told from the others
by its import path. A capability's tables are `adapters/tables.py`, so core's
are `tables.py` too.

`Base.metadata` is the *only* description of what is on disk, and Alembic
generates migrations by comparing against it. Nothing else may create or alter
a table: a schema built by anything but a migration is a schema no upgrade can
reason about.

The tables themselves are no longer all here. A capability's table lives beside
the store that reads it — `capabilities/sites/adapters/tables.py` and its three
siblings — and registers itself on this base by importing it, so there is still
one metadata and still one migration tree. What is left here is core's own:
fleet-wide Ansible settings and the audit log. Which table modules must be
imported for the metadata to be complete is answered in one place,
`migrations/env.py`, and `tests/platform/test_migrations.py` fails if that list
falls behind the tree.

What is a column, and what is JSON
----------------------------------
A field earns a column when something queries it: identity, foreign keys,
    lifecycle status, timestamps, and anything a ``WHERE`` or ``ORDER BY`` names
today. Nested value objects that no query reaches — a site's cache and firewall
policy, the ``AnsibleRun`` a deployment produced, or a workflow's steps — stay
whole in a JSON column.

That line is deliberate rather than lazy. Normalising a site policy into tables
would give the capability domain models a rival definition of
the same invariants, with nothing to keep the two honest. Storing everything as
one opaque blob instead would bury facts SQL needs to see — an edge's host and
port, a record's TTL. A column per queryable fact and JSON for the policy is
what keeps exactly one source of truth for each.

These models hold no invariants of their own. Validation belongs to the domain
models, which every store still round-trips through. The two rules above are
the schema's, not this module's: a capability's table module is held to them
just the same.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Column,
    Integer,
    String,
    TypeDecorator,
)
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.engine.interfaces import Dialect
from sqlmodel import Field, SQLModel


class UtcDateTime(TypeDecorator[datetime]):
    """A timezone-aware UTC instant, stored as the ISO-8601 text it always was.

    Not :class:`sqlalchemy.DateTime`. On SQLite that type stores
    ``YYYY-MM-DD HH:MM:SS.ffffff`` and drops the offset, which would do two
    unrelated kinds of damage here: every ``datetime.now(UTC)`` this control
    plane records would come back naive, and the on-disk text would stop being
    the ISO-8601 the Ansible inventory plugin already parses out of this file
    with the standard library.

    Storing the offset explicitly also keeps the column meaningful on a
    dialect with real timestamp support later — the values are unambiguous
    instants rather than wall-clock strings whose zone you have to know.
    """

    impl = String
    cache_ok = True

    def process_bind_param(
        self, value: datetime | None, dialect: Dialect
    ) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("refusing to store a naive datetime; attach UTC")
        return value.astimezone(UTC).isoformat()

    def process_result_value(
        self, value: str | None, dialect: Dialect
    ) -> datetime | None:
        if value is None:
            return None
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("stored timestamps must include a UTC offset")
        return parsed


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(SQLModel):
    """Declarative base for every table in the control-plane database."""

    __abstract__ = True


class AnsibleSettingRow(Base, table=True):
    """Non-secret, fleet-wide Ansible policy.

    ``value`` is JSON because the whole point is that an operator sets an
    arbitrary Ansible variable — a bool, an int, a list of CIDRs. A column per
    setting would mean a migration per setting.
    """

    __tablename__ = "ansible_settings"

    name: str = Field(sa_column=Column(String, primary_key=True))
    value: dict[str, Any] = Field(sa_type=JSON)
    updated_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)


class AuditEventRow(Base, table=True):
    """The append-only record of who changed what."""

    __tablename__ = "audit_events"

    id: int | None = Field(
        default=None,
        sa_column=Column(Integer, primary_key=True, autoincrement=True),
    )
    created_at: datetime = Field(
        default_factory=utcnow, sa_type=UtcDateTime, index=True
    )
    operator: str
    action: str
    resource_type: str
    resource_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict, sa_type=JSON)
