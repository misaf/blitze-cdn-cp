"""Initial dns schema.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import sqlite

import blitzecdn.core.persistence.tables

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "domains",
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("policy", sqlite.JSON(), nullable=False),
        sa.Column(
            "updated_at",
            blitzecdn.core.persistence.tables.UtcDateTime(),
            nullable=False,
        ),
        sa.CheckConstraint("length(name) > 0", name="domains_name_nonempty_check"),
        sa.PrimaryKeyConstraint("name"),
    )

    op.create_table(
        "zone_rules",
        sa.Column("domain", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("match", sa.String(), nullable=False),
        sa.Column("overrides", sqlite.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "updated_at",
            blitzecdn.core.persistence.tables.UtcDateTime(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["domain"], ["domains.name"], ondelete="CASCADE"),
        sa.CheckConstraint("length(name) > 0", name="zone_rules_name_nonempty_check"),
        sa.CheckConstraint("length(match) > 0", name="zone_rules_match_nonempty_check"),
        sa.CheckConstraint(
            "priority BETWEEN 1 AND 1000", name="zone_rules_priority_check"
        ),
        sa.PrimaryKeyConstraint("domain", "name"),
    )

    op.create_table(
        "dns_records",
        sa.Column("domain", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=False),
        sa.Column("ttl", sa.Integer(), nullable=False),
        sa.Column("proxied", sa.Boolean(), nullable=False),
        sa.Column(
            "updated_at",
            blitzecdn.core.persistence.tables.UtcDateTime(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["domain"], ["domains.name"], ondelete="CASCADE"),
        sa.CheckConstraint("ttl BETWEEN 1 AND 604800", name="dns_records_ttl_check"),
        sa.CheckConstraint("type IN ('A', 'AAAA')", name="dns_records_type_check"),
        sa.CheckConstraint("length(name) > 0", name="dns_records_name_nonempty_check"),
        sa.CheckConstraint(
            "value IS NOT NULL AND length(value) > 0",
            name="dns_records_value_check",
        ),
        sa.PrimaryKeyConstraint("domain", "name", "type"),
    )


def downgrade() -> None:
    op.drop_table("dns_records")

    op.drop_table("zone_rules")

    op.drop_table("domains")
