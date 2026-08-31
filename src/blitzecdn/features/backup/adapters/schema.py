"""The database's schema identity, read without opening the ORM.

A backup records the Alembic revision it was taken at, and a restore refuses a
revision this installation has never heard of. Both answers come from here so
"which schema" has one definition.
"""

from __future__ import annotations

import sqlite3
from functools import cache
from io import StringIO
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from blitzecdn.core.config import Settings

_MIGRATIONS = Path(__file__).parents[3] / "migrations"


@cache
def _revisions() -> frozenset[str]:
    """Every revision this installation's migration tree defines.

    Cached because it is the same answer for the life of the process and
    reading it walks a directory of Python modules.
    """
    config = Config(stdout=StringIO())
    config.set_main_option("script_location", str(_MIGRATIONS))
    return frozenset(
        script.revision
        for script in ScriptDirectory.from_config(config).walk_revisions()
    )


class AlembicSchemaVersions:
    """Reads `alembic_version` directly, so it works on a database at rest."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def current(self) -> str | None:
        return self.of(self._settings.database_path)

    @staticmethod
    def of(path: Path) -> str | None:
        """The revision stamped on a database file, or ``None``.

        Read-only and over `sqlite3` rather than the engine on purpose: this is
        asked *about* a file, sometimes one that is not the running database
        and must not be migrated by the act of being inspected.
        """
        if not path.is_file():
            return None
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error:
            return None
        try:
            row = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()
        except sqlite3.Error:
            return None
        finally:
            connection.close()
        return str(row[0]) if row else None

    @staticmethod
    def known(revision: str) -> bool:
        return revision in _revisions()


__all__ = ["AlembicSchemaVersions"]
