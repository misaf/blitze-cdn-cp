"""Persistence for deployment requirements and fleet-wide settings."""

from typing import Any, cast

from sqlalchemy import CursorResult, Result, delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import col

from blitzecdn.capabilities.deployments.domain import DeploymentRequirementKind
from blitzecdn.core.domain.validation import validate_setting_name
from blitzecdn.core.exceptions import NotFoundError
from blitzecdn.core.persistence.engine import Database
from blitzecdn.core.persistence.models import (
    AnsibleSettingRow,
    DeploymentRequirementRow,
)


class DeploymentRequirementStore:
    def __init__(self, database: Database) -> None:
        self._db = database

    def require(self, kind: DeploymentRequirementKind) -> None:
        with self._db.session() as session:
            session.execute(
                sqlite_insert(DeploymentRequirementRow)
                .values(kind=kind.value, requested_at=self._db.now())
                .on_conflict_do_update(
                    index_elements=[DeploymentRequirementRow.kind],
                    set_={"requested_at": self._db.now()},
                )
            )

    def clear(self, kind: DeploymentRequirementKind) -> None:
        with self._db.session() as session:
            session.execute(
                delete(DeploymentRequirementRow).where(
                    col(DeploymentRequirementRow.kind) == kind.value
                )
            )

    def pending(self, kind: DeploymentRequirementKind) -> bool:
        with self._db.session() as session:
            return session.get(DeploymentRequirementRow, kind.value) is not None


def _rows_affected(result: Result[Any]) -> int:
    """``rowcount`` for a DML statement.

    ``Session.execute`` is typed as returning ``Result``, which has no
    ``rowcount`` — only the ``CursorResult`` a DML statement actually produces
    does. The narrowing is here rather than at each call site so "how many rows
    did that touch" stays one readable expression.
    """
    return cast("CursorResult[Any]", result).rowcount


class AnsibleSettingStore:
    """Non-secret, fleet-wide Ansible policy stored by the control plane."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def list_settings(self) -> dict[str, Any]:
        with self._db.session() as session:
            rows = session.scalars(
                select(AnsibleSettingRow).order_by(AnsibleSettingRow.name)
            ).all()
            return {row.name: row.value for row in rows}

    def set_setting(self, name: str, value: Any) -> None:
        # Checked here rather than only at the entry point that happens to be
        # the sole writer today. These rows are published to every host at
        # inventory precedence, so the rule protects the fleet, not the CLI.
        name = validate_setting_name(name)
        with self._db.session() as session:
            statement = sqlite_insert(AnsibleSettingRow).values(
                name=name, value=value, updated_at=self._db.now()
            )
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=[AnsibleSettingRow.name],
                    set_={
                        "value": statement.excluded.value,
                        "updated_at": statement.excluded.updated_at,
                    },
                )
            )

    def delete_setting(self, name: str) -> None:
        name = validate_setting_name(name)
        with self._db.session() as session:
            result = _rows_affected(
                session.execute(
                    delete(AnsibleSettingRow).where(col(AnsibleSettingRow.name) == name)
                )
            )
            if result == 0:
                raise NotFoundError(f"Ansible setting {name!r} does not exist")


__all__ = ["AnsibleSettingStore", "DeploymentRequirementStore"]
