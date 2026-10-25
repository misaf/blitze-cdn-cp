"""Persistence for the fleet-wide Ansible settings core itself owns."""

from collections.abc import Mapping
from typing import Any, cast

from sqlalchemy import CursorResult, Result, delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import col

from blitzecdn.core.domain.validation import validate_setting_name
from blitzecdn.core.exceptions import NotFoundError
from blitzecdn.core.persistence.engine import Database
from blitzecdn.core.persistence.tables import AnsibleSettingRow


def _rows_affected(result: Result[Any]) -> int:
    """``rowcount`` for a DML statement, narrowed from ``Result`` once, here."""
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
        # Checked in the store, not the CLI: these rows reach every host.
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

    def replace_all_settings(self, settings: Mapping[str, Any]) -> None:
        """Restore the table wholesale for a rollback, revalidating every name."""
        names = {name: validate_setting_name(name) for name in settings}
        now = self._db.now()
        with self._db.session() as session:
            session.execute(delete(AnsibleSettingRow))
            session.flush()
            session.add_all(
                [
                    AnsibleSettingRow(name=names[name], value=value, updated_at=now)
                    for name, value in sorted(settings.items())
                ]
            )


__all__ = ["AnsibleSettingStore"]
