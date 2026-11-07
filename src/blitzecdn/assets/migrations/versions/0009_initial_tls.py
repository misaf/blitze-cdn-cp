"""Initial TLS schema.

Revision ID: 0009_tls
Revises: 0008_traffic
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_tls"
down_revision = "0008_traffic"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "certificate_preflight_attempts",
        sa.Column("site", sa.String(), nullable=False),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("checked_at", sa.String(), nullable=False),
        sa.Column("next_check_at", sa.String(), nullable=False),
        sa.Column("summary", sa.String(), nullable=False),
        sa.CheckConstraint(
            "failures >= 1", name="certificate_preflight_attempts_failures_check"
        ),
        sa.PrimaryKeyConstraint("site"),
    )


def downgrade() -> None:
    op.drop_table("certificate_preflight_attempts")
