"""Initial releases schema.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import sqlite

import blitzecdn.core.persistence.tables

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "release_inputs",
        sa.Column("digest", sa.String(), nullable=False),
        sa.Column("document", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            blitzecdn.core.persistence.tables.UtcDateTime(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("digest"),
    )

    op.create_table(
        "releases",
        sa.Column("digest", sa.String(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("compiler_version", sa.Integer(), nullable=False),
        sa.Column("inputs_digest", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            blitzecdn.core.persistence.tables.UtcDateTime(),
            nullable=False,
        ),
        sa.Column("document", sqlite.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["inputs_digest"],
            ["release_inputs.digest"],
        ),
        sa.PrimaryKeyConstraint("digest"),
    )

    with op.batch_alter_table("releases", schema=None) as batch_op:
        batch_op.create_index("ix_releases_created_at", ["created_at"], unique=False)
        batch_op.create_index("ix_releases_id", ["id"], unique=True)

    op.create_table(
        "release_configurations",
        sa.Column("release_digest", sa.String(), nullable=False),
        sa.Column("digest", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            blitzecdn.core.persistence.tables.UtcDateTime(),
            nullable=False,
        ),
        sa.Column("document", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["release_digest"], ["releases.digest"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("release_digest"),
    )


def downgrade() -> None:

    op.drop_table("release_configurations")

    op.drop_table("releases")

    op.drop_table("release_inputs")
