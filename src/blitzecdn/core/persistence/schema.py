"""The database's schema identity, read without opening the ORM.

Core owns this because core owns the schema. A backup records the Alembic
revision it was taken at and a restore refuses a revision this installation has
never heard of, but "which revision is this file stamped with" is a question
about the engine beside it, not about tar files — and asking it must never migrate
the file being asked about, which is why it is read over `sqlite3` in read-only
mode rather than through the engine.

:data:`MIGRATIONS_PATH` is the one place the migration tree is located. The
engine reaches for the same constant, and an installed plugin that needs the
schema identity gets it from here instead of computing a path relative to a
module that may not live inside this distribution at all.
"""

from __future__ import annotations

import sqlite3
from functools import cache
from io import StringIO
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from blitzecdn.core.config import Settings
from blitzecdn.core.runtime.resources import package_directory

#: The Alembic tree this installation migrates with. Located through the same
#: helper the Ansible tree uses rather than by counting ``..`` from
#: ``__file__``: that count was silently wrong the moment this module moved one
#: directory deeper, and it is exactly the idiom `runtime.resources` exists to
#: replace.
MIGRATIONS_PATH = (
    package_directory("blitzecdn", resolves="Alembic reads its migration tree by path")
    / "migrations"
)


@cache
def _revisions() -> frozenset[str]:
    """Every revision this installation's migration tree defines.

    Cached because it is the same answer for the life of the process and
    reading it walks a directory of Python modules.
    """
    config = Config(stdout=StringIO())
    config.set_main_option("script_location", str(MIGRATIONS_PATH))
    return frozenset(
        script.revision
        for script in ScriptDirectory.from_config(config).walk_revisions()
    )


class DatabaseSchema:
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

    @staticmethod
    def migrate(path: Path) -> None:
        """Bring one database file up to this installation's head revision.

        Core's job, not a caller's. A restore puts back a database taken at an
        older revision and has to forward-migrate it before anything reads it,
        but "how this schema moves" is owned here — the alternative was the
        backup adapter importing `persistence.engine` directly, which made a
        detachable package depend on the storage implementation rather than on
        a contract.

        The engine is imported inside the call for two reasons: it imports
        :data:`MIGRATIONS_PATH` from this module, so a module-scope import
        would be a cycle; and opening it costs Alembic, which every command
        that only wants to *read* a revision should not pay.
        """
        from blitzecdn.core.persistence.engine import Database

        Database(path).close()


__all__ = ["MIGRATIONS_PATH", "DatabaseSchema"]
