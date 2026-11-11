"""Record which release the fleet ran when each deployment was queued.

Revision ID: 0013_deployment_previous_release
Revises: 0012_path_rules
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_deployment_previous_release"
down_revision = "0012_path_rules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable: nothing was live before the first deployment, and existing rows
    # were queued before anyone asked.
    with op.batch_alter_table("deployments") as batch:
        batch.add_column(
            sa.Column(
                "previous_release_id",
                sa.String(),
                sa.ForeignKey("releases.id", name="fk_deployments_previous_release_id"),
                nullable=True,
            )
        )
        batch.create_index(
            "ix_deployments_previous_release_id", ["previous_release_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("deployments") as batch:
        batch.drop_index("ix_deployments_previous_release_id")
        batch.drop_column("previous_release_id")
