"""Initial workflows schema.

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import sqlite

import blitzecdn.core.persistence.tables

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workflows",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("resource_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("operator", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            blitzecdn.core.persistence.tables.UtcDateTime(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            blitzecdn.core.persistence.tables.UtcDateTime(),
            nullable=False,
        ),
        sa.Column("steps", sqlite.JSON(), nullable=False),
        sa.Column("error", sa.String(), nullable=True),
        sa.CheckConstraint(
            "kind <> '' AND length(kind) <= 64 AND kind = lower(kind)",
            name="workflows_kind_check",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'needs_review')",
            name="workflows_status_check",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    with op.batch_alter_table("workflows", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_workflows_created_at"), ["created_at"], unique=False
        )
        batch_op.create_index(
            "workflows_status_idx", ["status", "created_at"], unique=False
        )


def downgrade() -> None:

    op.drop_table("workflows")
