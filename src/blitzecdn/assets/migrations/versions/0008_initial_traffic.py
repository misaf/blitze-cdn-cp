"""Initial traffic schema.

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa
from alembic import op

import blitzecdn.core.persistence.tables

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "traffic_hours",
        sa.Column("edge", sa.String(), nullable=False),
        sa.Column("hostname", sa.String(), nullable=False),
        sa.Column(
            "hour", blitzecdn.core.persistence.tables.UtcDateTime(), nullable=False
        ),
        sa.Column("requests", sa.Integer(), nullable=False),
        sa.Column("bytes_sent", sa.Integer(), nullable=False),
        sa.Column("body_bytes_sent", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "requests > 0 AND bytes_sent >= 0 AND body_bytes_sent >= 0",
            name="traffic_hours_counters_check",
        ),
        sa.PrimaryKeyConstraint("edge", "hostname", "hour"),
    )

    op.create_index(
        "ix_traffic_hours_hour_hostname_edge",
        "traffic_hours",
        ["hour", "hostname", "edge"],
        unique=False,
    )

    op.create_table(
        "traffic_cursors",
        sa.Column("edge", sa.String(), nullable=False),
        sa.Column("stream_id", sa.String(), nullable=False),
        sa.Column("offset", sa.Integer(), nullable=False),
        sa.Column("anchor", sa.String(), nullable=False),
        sa.Column(
            "latest_hour",
            blitzecdn.core.persistence.tables.UtcDateTime(),
            nullable=False,
        ),
        sa.CheckConstraint("offset > 0", name="traffic_cursors_offset_check"),
        sa.PrimaryKeyConstraint("edge", "stream_id"),
    )

    op.create_table(
        "traffic_collections",
        sa.Column("edge", sa.String(), nullable=False),
        sa.Column("attempt_id", sa.String(), nullable=False),
        sa.Column(
            "started_at",
            blitzecdn.core.persistence.tables.UtcDateTime(),
            nullable=False,
        ),
        sa.Column(
            "finished_at",
            blitzecdn.core.persistence.tables.UtcDateTime(),
            nullable=True,
        ),
        sa.Column(
            "last_success_at",
            blitzecdn.core.persistence.tables.UtcDateTime(),
            nullable=True,
        ),
        sa.Column(
            "last_caught_up_at",
            blitzecdn.core.persistence.tables.UtcDateTime(),
            nullable=True,
        ),
        sa.Column("pending", sa.Boolean(), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("edge"),
    )

    op.create_table(
        "traffic_retention",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "cutoff", blitzecdn.core.persistence.tables.UtcDateTime(), nullable=False
        ),
        sa.CheckConstraint("id = 1", name="traffic_retention_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("traffic_collections")

    op.drop_table("traffic_retention")

    op.drop_table("traffic_cursors")

    op.drop_table("traffic_hours")
