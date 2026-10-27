"""The declarative base every table uses, and core's own tables.

`Base.metadata` is the only description of what is on disk; Alembic diffs
against it and nothing but a migration creates or alters a table. Capability
tables live in `adapters/tables.py` beside their stores and register on this
base; `migrations/env.py` imports them all, checked by
`tests/platform/test_migrations.py`.

A field gets a column when something queries it — identity, foreign keys,
status, timestamps, anything in a ``WHERE`` or ``ORDER BY``. Value objects no
query reaches, such as a site's policy or an ``AnsibleRun``, stay whole in a
JSON column, so the domain model stays their only definition. Tables hold no
invariants; stores round-trip through the domain models.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import String, TypeDecorator
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class UtcDateTime(TypeDecorator[datetime]):
    """A timezone-aware UTC instant, stored as ISO-8601 text.

    SQLite's ``DateTime`` drops the offset, so values would come back naive, and
    the inventory plugin parses this text with the standard library.
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


class Base(DeclarativeBase):
    """Declarative base for every table in the control-plane database."""


class AnsibleSettingRow(Base):
    """Non-secret, fleet-wide Ansible policy; ``value`` is JSON, any Ansible type."""

    __tablename__ = "ansible_settings"

    name: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class AuditEventRow(Base):
    """The append-only record of who changed what."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    operator: Mapped[str] = mapped_column()
    action: Mapped[str] = mapped_column()
    resource_type: Mapped[str] = mapped_column()
    resource_id: Mapped[str | None] = mapped_column()
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
