"""Status classes and top paths per traffic hour, for per-site analytics.

Revision ID: 0011_traffic_analytics
Revises: 0010_deployment_outcomes
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_traffic_analytics"
down_revision = "0010_deployment_outcomes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "traffic_status_hours",
        sa.Column("edge", sa.String(), nullable=False),
        sa.Column("hostname", sa.String(), nullable=False),
        sa.Column("hour", sa.String(), nullable=False),
        sa.Column("status_class", sa.String(), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False),
        sa.CheckConstraint("requests > 0", name="traffic_status_hours_requests_check"),
        sa.CheckConstraint(
            "status_class IN ('1xx', '2xx', '3xx', '4xx', '5xx')",
            name="traffic_status_hours_class_check",
        ),
        sa.ForeignKeyConstraint(
            ["edge", "hostname", "hour"],
            ["traffic_hours.edge", "traffic_hours.hostname", "traffic_hours.hour"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("edge", "hostname", "hour", "status_class"),
    )
    op.create_index(
        "ix_traffic_status_hours_hour_hostname",
        "traffic_status_hours",
        ["hour", "hostname"],
        unique=False,
    )

    op.create_table(
        "traffic_path_hours",
        sa.Column("edge", sa.String(), nullable=False),
        sa.Column("hostname", sa.String(), nullable=False),
        sa.Column("hour", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False),
        sa.Column("bytes_sent", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "requests > 0 AND bytes_sent >= 0",
            name="traffic_path_hours_counters_check",
        ),
        sa.ForeignKeyConstraint(
            ["edge", "hostname", "hour"],
            ["traffic_hours.edge", "traffic_hours.hostname", "traffic_hours.hour"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("edge", "hostname", "hour", "path"),
    )
    op.create_index(
        "ix_traffic_path_hours_hour_hostname",
        "traffic_path_hours",
        ["hour", "hostname"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("traffic_path_hours")

    op.drop_table("traffic_status_hours")
