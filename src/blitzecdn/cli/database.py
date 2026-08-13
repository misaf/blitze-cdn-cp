"""Database maintenance commands for the control plane."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy import create_engine

from blitzecdn.cli import common

database_app = typer.Typer(no_args_is_help=True, help="Maintain the database.")


@database_app.command("backup")
def backup(
    destination: Annotated[
        Path | None,
        typer.Argument(help="Where to write the copy; defaults to a timestamped file."),
    ] = None,
) -> None:
    """Copy the database to a consistent file, safely, while it is in use.

    Not `cp`. The database runs in WAL mode, so the `.db` file on its own is
    not the current state — recent commits live in the `-wal` beside it, and a
    plain copy of the one without the other is a backup of some earlier moment
    that will not say so. `VACUUM INTO` asks SQLite to write a complete
    database, which means this is safe to run against a controller that is
    serving, and produces one file rather than three.

    Nothing schedules this. Run it on whatever cadence the data is worth.
    """
    path = common.settings().database_path
    if not path.exists():
        raise typer.BadParameter(f"{path} does not exist; nothing to back up")
    target = destination or path.with_name(
        f"{path.stem}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.db"
    )
    if target.exists():
        raise typer.BadParameter(f"{target} already exists; refusing to overwrite")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    engine = create_engine(f"sqlite+pysqlite:///{path}")
    try:
        with engine.connect() as connection:
            # Bound as a parameter rather than interpolated: a path is operator
            # input, and this one reaches SQL.
            connection.exec_driver_sql("VACUUM INTO ?", (str(target),))
    finally:
        engine.dispose()
    # The database holds API keys' operator names, certificate metadata and the
    # whole audit trail, so the copy is as sensitive as the original.
    target.chmod(0o600)
    typer.echo(f"Backed up {path} -> {target}")
