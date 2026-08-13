"""Record the canonical state a rollback started from.

A rollback restores zones wholesale, and record writes do not take the
deployment lock — so a record created while a rollback was converging was
deleted by the adoption that followed it, with no conflict and nothing in the
audit trail to say it had existed. The column added here is what adoption
compares against so that case becomes a refusal instead.

Nullable, and left null on every existing row: it only means anything for a
rollback, and no rollback recorded before this migration is still running.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("deployments", schema=None) as batch_op:
        batch_op.add_column(sa.Column("canonical_digest", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("deployments", schema=None) as batch_op:
        batch_op.drop_column("canonical_digest")
