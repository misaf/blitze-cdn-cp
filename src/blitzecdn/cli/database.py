"""Schema migrations for the control-plane database.

Thin wrappers over Alembic. They exist so an operator never has to know that
Alembic is what runs, or to be standing in the right directory with the right
``ALEMBIC_CONFIG`` for it to find the migration scripts — both of which are
easy to get wrong on a server, and one of which silently migrates the wrong
file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine

from blitzecdn.cli import common
from blitzecdn.infrastructure.engine import SCHEMA_REVISION

database_app = typer.Typer(no_args_is_help=True, help="Migrate the database schema.")


def _alembic() -> Config:
    """Alembic pointed at the migrations shipped inside this package."""
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parent.parent / "migrations")
    )
    return config


def current_revision(path: Path) -> str | None:
    """The revision ``path`` is stamped with, or ``None`` if it is unstamped."""
    engine = create_engine(f"sqlite+pysqlite:///{path}")
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def upgrade_to_head(path: Path) -> None:
    """Migrate ``path`` to the newest revision.

    Also called by ``blitzecdn setup``, which is what ``install.sh`` runs on an
    update — so an operator following the documented upgrade never has to know
    a migration was needed.
    """
    config = _alembic()
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{path}")
    command.upgrade(config, "head")


@database_app.command("current")
def current() -> None:
    """Show the schema revision this database is on."""
    path = common.settings().database_path
    if not path.exists():
        typer.echo(f"{path} does not exist yet; it is created on first use.")
        return
    revision = current_revision(path)
    typer.echo(
        f"{path}: {revision or 'unstamped'} (this release expects {SCHEMA_REVISION})"
    )
    if revision != SCHEMA_REVISION:
        typer.echo("Run 'blitzecdn db upgrade' to migrate it.")


@database_app.command("upgrade")
def upgrade(
    revision: Annotated[
        str, typer.Option(help="Target revision; 'head' is the newest.")
    ] = "head",
) -> None:
    """Migrate the database to the schema this release expects.

    Take a copy of the file first. The expansion migration rewrites tables
    rather than altering them in place — SQLite cannot do otherwise — so an
    interrupted run is much easier to recover from a copy than from the
    original.
    """
    path = common.settings().database_path
    if not path.exists():
        raise typer.BadParameter(f"{path} does not exist; nothing to migrate")
    before = current_revision(path)
    config = _alembic()
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{path}")
    command.upgrade(config, revision)
    typer.echo(f"Migrated {path}: {before or 'unstamped'} -> {current_revision(path)}")
