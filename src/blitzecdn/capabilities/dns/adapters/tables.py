"""The zones, their rules, and their records, stored."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Column, ForeignKey, String
from sqlalchemy.dialects.sqlite import JSON
from sqlmodel import Field

from blitzecdn.core.persistence.tables import Base, UtcDateTime, utcnow


class DomainRow(Base, table=True):
    """A delegated zone and its policy, kept whole as JSON; records are keyed below."""

    __tablename__ = "domains"
    __table_args__ = (
        CheckConstraint("length(name) > 0", name="domains_name_nonempty_check"),
    )

    name: str = Field(sa_column=Column(String, primary_key=True))
    policy: dict[str, Any] = Field(default_factory=dict, sa_type=JSON)
    updated_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)


class DnsRecordRow(Base, table=True):
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
