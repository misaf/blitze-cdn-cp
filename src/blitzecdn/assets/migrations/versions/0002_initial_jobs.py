"""Initial jobs schema.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import sqlite

import blitzecdn.core.persistence.tables

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("payload", sqlite.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("dedupe_key", sa.String(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column(
            "available_at",
            blitzecdn.core.persistence.tables.UtcDateTime(),
            nullable=False,
        ),
        sa.Column(
            "leased_until",
            blitzecdn.core.persistence.tables.UtcDateTime(),
            nullable=True,
        ),
        sa.Column("leased_by", sa.String(), nullable=True),
        sa.Column("fence", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            blitzecdn.core.persistence.tables.UtcDateTime(),
            nullable=False,
        ),
        sa.Column(
            "finished_at",
            blitzecdn.core.persistence.tables.UtcDateTime(),
            nullable=True,
        ),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("result", sqlite.JSON(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')",
            name="jobs_status_check",
        ),
        sa.CheckConstraint("attempts >= 0", name="jobs_attempts_check"),
        sa.CheckConstraint("max_attempts >= 1", name="jobs_max_attempts_check"),
        sa.PrimaryKeyConstraint("id"),
    )

    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_jobs_created_at"), ["created_at"], unique=False
        )
        # Partial: single-flight binds only while a job is unfinished.
        batch_op.create_index(
            "ix_jobs_dedupe_key_unfinished",
            ["dedupe_key"],
            unique=True,
            sqlite_where=sa.text("status IN ('pending', 'running')"),
        )
        batch_op.create_index("ix_jobs_leased_until", ["leased_until"], unique=False)
        batch_op.create_index(
            "ix_jobs_status_available_at", ["status", "available_at"], unique=False
        )

    op.create_table(
        "job_schedules",
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("jitter_seconds", sa.Integer(), nullable=False),
        sa.Column("lease_seconds", sa.Integer(), nullable=False),
        sa.Column(
            "next_run_at",
            blitzecdn.core.persistence.tables.UtcDateTime(),
            nullable=False,
        ),
        sa.Column(
            "last_run_at",
            blitzecdn.core.persistence.tables.UtcDateTime(),
            nullable=True,
        ),
        sa.CheckConstraint(
            "interval_seconds >= 1", name="job_schedules_interval_check"
        ),
        sa.CheckConstraint("lease_seconds >= 1", name="job_schedules_lease_check"),
        sa.PrimaryKeyConstraint("name"),
    )

    with op.batch_alter_table("job_schedules", schema=None) as batch_op:
        batch_op.create_index(
            "ix_job_schedules_next_run_at", ["next_run_at"], unique=False
        )


def downgrade() -> None:

    op.drop_table("job_schedules")

    op.drop_table("jobs")
