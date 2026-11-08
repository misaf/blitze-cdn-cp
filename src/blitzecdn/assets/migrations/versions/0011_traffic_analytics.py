"""Count response status classes and the busiest paths per site and hour.

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

STATUS_COLUMNS = ("status_1xx", "status_2xx", "status_3xx", "status_4xx", "status_5xx")


def upgrade() -> None:
    for name in STATUS_COLUMNS:
        op.add_column(
            "traffic_hours",
            sa.Column(name, sa.Integer(), nullable=False, server_default="0"),
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
        "ix_traffic_path_hours_hostname_hour",
        "traffic_path_hours",
        ["hostname", "hour"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_traffic_path_hours_hostname_hour", table_name="traffic_path_hours"
    )
    op.drop_table("traffic_path_hours")
    for name in reversed(STATUS_COLUMNS):
        op.drop_column("traffic_hours", name)
