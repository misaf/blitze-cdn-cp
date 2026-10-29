"""Schema identity, read-only validation and explicit migrations.

Reading a file's revision goes over read-only `sqlite3`, never the engine, so
inspecting a file — a backup's, say — can never migrate it.
"""

from __future__ import annotations

import fcntl
import sqlite3
import threading
from functools import cache
from io import StringIO
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError

from blitzecdn.core.config import Settings
from blitzecdn.core.exceptions import ConfigurationError
from blitzecdn.core.runtime.resources import package_directory

#: The Alembic tree this installation migrates with.
MIGRATIONS_PATH = (
    package_directory(
        "blitzecdn.assets", resolves="Alembic reads its migration tree by path"
    )
    / "migrations"
)


# Alembic installs a process-global environment proxy while commands run.
# File locks serialize migrators across processes; this lock also protects
# concurrent read-only validation and migration calls within one process.
_alembic_lock = threading.RLock()


@cache
def _revisions() -> frozenset[str]:
    """Every revision the migration tree defines, cached for the process."""
    config = Config(stdout=StringIO())
    config.set_main_option("script_location", str(MIGRATIONS_PATH))
    return frozenset(
        script.revision
        for script in ScriptDirectory.from_config(config).walk_revisions()
    )


class DatabaseSchema:
    """Own schema inspection and maintenance independently of repositories."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def current(self) -> str | None:
        return self.of(self._settings.database_path)

    @staticmethod
    def of(path: Path) -> str | None:
        """The revision stamped on a database file, or ``None``; never migrates it."""
        if not path.is_file():
            return None
        try:
            connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
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
    def validate(path: Path) -> None:
        """Refuse absent, stale or damaged schemas without changing anything."""
        if not path.is_file():
            raise ConfigurationError(
                f"{path} does not exist. Run `blitzecdn setup` for a new controller "
                "or restore its backup before starting services."
            )
        config = _config(path, read_only=True)
        try:
            with _alembic_lock:
                command.check(config)
        except (CommandError, RuntimeError, SQLAlchemyError) as exc:
            raise _schema_error(path, exc) from exc

    @staticmethod
    def migrate(path: Path) -> None:
        """Initialize or upgrade a stopped controller's database, under a file lock."""
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        migration_lock = path.with_name(f".{path.name}.migration.lock")
        with _alembic_lock, migration_lock.open("a+b") as lock_file:
            migration_lock.chmod(0o600)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            # Alembic resolves a revision by unique prefix, so a retired id
            # such as "0001" would silently mean "0001_core"; demand an exact one.
            stamped = DatabaseSchema.of(path)
            if stamped is not None and not DatabaseSchema.known(stamped):
                raise _schema_error(
                    path, CommandError(f"unknown schema revision {stamped!r}")
                )
            config = _config(path)
            try:
                command.upgrade(config, "head")
                command.check(config)
            except (CommandError, RuntimeError, SQLAlchemyError) as exc:
                raise _schema_error(path, exc) from exc


def _config(path: Path, *, read_only: bool = False) -> Config:
    config = Config(stdout=StringIO())
    config.set_main_option("script_location", str(MIGRATIONS_PATH))
    url = URL.create(
        "sqlite+pysqlite",
        database=path.resolve().as_uri(),
        query={"mode": "ro" if read_only else "rwc", "uri": "true"},
    ).render_as_string(hide_password=False)
    # Alembic uses ConfigParser interpolation; percent-encoded paths must
    # survive it before SQLAlchemy decodes the SQLite URI.
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def _schema_error(path: Path, exc: Exception) -> ConfigurationError:
    return ConfigurationError(
        f"{path} has an incompatible or damaged schema: {exc}. "
        "Stop the controller services, preserve a backup and run `blitzecdn migrate`. "
        "If validation still fails, restore a verified backup."
    )


__all__ = ["MIGRATIONS_PATH", "DatabaseSchema"]
