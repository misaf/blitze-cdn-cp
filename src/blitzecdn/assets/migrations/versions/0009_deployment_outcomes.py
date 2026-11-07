"""Record the running outcome of each kind of convergence.

Revision ID: 0009_deployment_outcomes
Revises: 0008_traffic
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_deployment_outcomes"
down_revision = "0008_traffic"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deployment_outcomes",
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("last_success_at", sa.String(), nullable=True),
        sa.Column("last_failure_at", sa.String(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('applied', 'drift_check')",
            name="deployment_outcomes_kind_check",
        ),
        sa.CheckConstraint(
            "consecutive_failures >= 0",
            name="deployment_outcomes_failures_check",
        ),
        sa.PrimaryKeyConstraint("kind"),
    )


def downgrade() -> None:
    op.drop_table("deployment_outcomes")
