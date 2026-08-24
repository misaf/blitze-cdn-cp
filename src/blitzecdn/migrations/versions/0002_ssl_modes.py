"""Replace the two origin schemes with combined SSL modes.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-24
"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _upgrade_policy(policy: dict[str, Any]) -> dict[str, Any]:
    if "ssl_mode" in policy:
        policy.pop("origin_scheme", None)
        return policy
    scheme = policy.pop("origin_scheme", "https")
    if policy.get("certificate_mode", "disabled") == "disabled":
        policy["ssl_mode"] = "off"
    elif scheme == "http":
        policy["ssl_mode"] = "flexible"
    else:
        policy["ssl_mode"] = "full_strict"
    return policy


def _downgrade_policy(policy: dict[str, Any]) -> dict[str, Any]:
    mode = policy.pop("ssl_mode", "off")
    policy["origin_scheme"] = "https" if mode in {"full", "full_strict"} else "http"
    return policy


def _rewrite(upgrade: bool) -> None:
    connection = op.get_bind()
    transform = _upgrade_policy if upgrade else _downgrade_policy
    for table, keys in (
        ("dns_records", ("domain", "name", "type")),
        ("sites", ("name",)),
    ):
        key_columns: list[Any] = [sa.column(key) for key in keys]
        policy_column: Any = sa.column("policy")
        policy_table = sa.table(table, *key_columns, policy_column)
        rows = connection.execute(
            sa.select(*key_columns, policy_column).select_from(policy_table)
        ).mappings()
        for row in rows:
            raw = row["policy"]
            policy = json.loads(raw) if isinstance(raw, str) else dict(raw)
            connection.execute(
                sa.update(policy_table)
                .where(sa.and_(*(policy_table.c[key] == row[key] for key in keys)))
                .values(policy=json.dumps(transform(policy), separators=(",", ":")))
            )


def upgrade() -> None:
    _rewrite(True)


def downgrade() -> None:
    _rewrite(False)
