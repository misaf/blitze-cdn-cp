"""The zones, their rules, and their records, stored."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from blitzecdn.core.persistence.tables import Base, UtcDateTime, utcnow


class DomainRow(Base):
    """A delegated zone and its policy, kept whole as JSON; records are keyed below."""

    __tablename__ = "domains"
    __table_args__ = (
        CheckConstraint("length(name) > 0", name="domains_name_nonempty_check"),
    )

    name: Mapped[str] = mapped_column(primary_key=True)
    policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class DnsRecordRow(Base):
    """One record: an address, and whether the edge serves the hostname.

    ``value`` is the DNS answer when unproxied and the origin when proxied. The
    check constraints repeat the domain rules because restores and rollbacks write
    rows without the editor.
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

    # A record outliving its zone would keep deriving a virtual host for it.
    domain: Mapped[str] = mapped_column(
        ForeignKey("domains.name", ondelete="CASCADE"), primary_key=True
    )
    name: Mapped[str] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[str] = mapped_column()
    ttl: Mapped[int] = mapped_column()
    proxied: Mapped[bool] = mapped_column(default=True)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class RuleRow(Base):
    """One override in a zone; ``match`` and ``priority`` are columns for ordering."""

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
    domain: Mapped[str] = mapped_column(
        ForeignKey("domains.name", ondelete="CASCADE"), primary_key=True
    )
    name: Mapped[str] = mapped_column(primary_key=True)
    priority: Mapped[int] = mapped_column()
    match: Mapped[str] = mapped_column()
    overrides: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(default=True)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
