"""Remember the Idempotency-Key a queued API request was sent with.

Revision ID: 0013_idempotency_keys
Revises: 0012_path_rules
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_idempotency_keys"
down_revision = "0012_path_rules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "idempotency_keys",
        sa.Column("operator", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column("resource_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("operator", "key"),
    )
    with op.batch_alter_table("idempotency_keys", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_idempotency_keys_created_at"), ["created_at"], unique=False
        )


def downgrade() -> None:
    op.drop_table("idempotency_keys")
