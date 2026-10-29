"""Initial edges schema.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import sqlite

import blitzecdn.core.persistence.tables

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "edges",
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("host", sa.String(), nullable=False),
        sa.Column("user", sa.String(), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("private_key_file", sa.String(), nullable=True),
        sa.Column("public_addresses", sqlite.JSON(), nullable=False),
        sa.Column("ssh_sources", sqlite.JSON(), nullable=False),
        sa.Column("capabilities", sqlite.JSON(), nullable=True),
        sa.Column(
            "updated_at",
            blitzecdn.core.persistence.tables.UtcDateTime(),
            nullable=False,
        ),
        sa.CheckConstraint("length(name) > 0", name="edges_name_nonempty_check"),
        sa.CheckConstraint("port BETWEEN 1 AND 65535", name="edges_port_check"),
        sa.CheckConstraint("length(host) > 0", name="edges_host_nonempty_check"),
        sa.CheckConstraint("length(user) > 0", name="edges_user_nonempty_check"),
        sa.PrimaryKeyConstraint("name"),
    )


def downgrade() -> None:
    op.drop_table("edges")
