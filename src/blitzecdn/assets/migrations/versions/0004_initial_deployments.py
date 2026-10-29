"""Initial deployments schema.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import sqlite

import blitzecdn.core.persistence.tables

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deployments",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("operator", sa.String(), nullable=False),
        sa.Column("check_mode", sa.Boolean(), nullable=False),
        sa.Column("rollback_of", sa.String(), nullable=True),
        sa.Column("host_limit", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            blitzecdn.core.persistence.tables.UtcDateTime(),
            nullable=False,
        ),
        sa.Column(
            "started_at", blitzecdn.core.persistence.tables.UtcDateTime(), nullable=True
        ),
        sa.Column(
            "finished_at",
            blitzecdn.core.persistence.tables.UtcDateTime(),
            nullable=True,
        ),
        sa.Column("result", sqlite.JSON(), nullable=True),
        sa.Column("release_id", sa.String(), nullable=False),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("canonical_digest", sa.String(), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["release_id"],
            ["releases.id"],
        ),
        sa.ForeignKeyConstraint(
            ["rollback_of"],
            ["deployments.id"],
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', "
            "'timed_out', 'abandoned')",
            name="deployments_status_check",
        ),
        sa.CheckConstraint(
            "rollback_of IS NULL OR rollback_of != id",
            name="deployments_no_self_rollback_check",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    with op.batch_alter_table("deployments", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_deployments_created_at"), ["created_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_deployments_rollback_of"), ["rollback_of"], unique=False
        )
        batch_op.create_index("ix_deployments_release_id", ["release_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_deployments_status_created_at"),
            ["status", "created_at"],
            unique=False,
        )

    op.create_table(
        "deployment_targets",
        sa.Column("deployment_id", sa.String(), nullable=False),
        sa.Column("edge", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("phase", sa.String(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("fence", sa.Integer(), nullable=False),
        sa.Column(
            "started_at",
            blitzecdn.core.persistence.tables.UtcDateTime(),
            nullable=True,
        ),
        sa.Column(
            "finished_at",
            blitzecdn.core.persistence.tables.UtcDateTime(),
            nullable=True,
        ),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["deployment_id"], ["deployments.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'skipped')",
            name="deployment_targets_status_check",
        ),
        sa.CheckConstraint(
            "phase IS NULL OR phase IN "
            "('prepare', 'validate', 'stage', 'activate', 'verify')",
            name="deployment_targets_phase_check",
        ),
        sa.PrimaryKeyConstraint("deployment_id", "edge"),
    )

    with op.batch_alter_table("deployment_targets", schema=None) as batch_op:
        batch_op.create_index(
            "ix_deployment_targets_deployment_id", ["deployment_id"], unique=False
        )

    op.create_table(
        "deployment_requirements",
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column(
            "requested_at",
            blitzecdn.core.persistence.tables.UtcDateTime(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("kind"),
    )


def downgrade() -> None:
    op.drop_table("deployment_requirements")

    op.drop_table("deployment_targets")

    op.drop_table("deployments")
