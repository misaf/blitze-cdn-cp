"""Let a rule match a request path, and redirect what it matches.

Revision ID: 0012_path_rules
Revises: 0011_traffic_analytics
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import sqlite

revision = "0012_path_rules"
down_revision = "0011_traffic_analytics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Both nullable: every existing rule is a hostname rule, which has neither.
    op.add_column("zone_rules", sa.Column("path", sa.String(), nullable=True))
    op.add_column("zone_rules", sa.Column("redirect", sqlite.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("zone_rules") as batch:
        batch.drop_column("redirect")
        batch.drop_column("path")
