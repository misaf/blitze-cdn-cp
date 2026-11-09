"""First-run commands: scaffold the local configuration a controller needs.

No inventory file: Ansible reads the fleet through the inventory plugin.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Annotated

import typer

from blitzecdn.composition import migrate_database
from blitzecdn.core.runtime.filesystem import create_private_file
from blitzecdn.entrypoints.cli import common
from blitzecdn.entrypoints.cli.root import app


def _environment_file_body() -> str:
    """The local API credential; non-secret settings belong in TOML."""
    return f"BLITZE_API_KEYS=local:{secrets.token_urlsafe(48)}\n"


@app.command()
def migrate() -> None:
    """Initialize or upgrade the database while controller services are stopped."""
    migrate_database(common.settings())
    typer.echo("Database schema is current.")


def _create_environment_file(path: Path) -> bool:
    """Write the env file with mode 0600; False when one already exists.

    An existing file or symlink is never overwritten or followed.
    """
    try:
        create_private_file(path, _environment_file_body())
    except FileExistsError:
        return False
    return True


@app.command()
def setup(
    env_file: Annotated[
        Path, typer.Option(help="Environment file to create if it is missing.")
    ] = Path(".env"),
) -> None:
    """Initialize the database and scaffold local configuration; safe to re-run."""
    migrate_database(common.settings())
    if _create_environment_file(env_file):
        typer.echo(f"BlitzeCDN is ready. Created: {env_file} (mode 0600)")
    else:
        typer.echo("BlitzeCDN is already set up; existing files were preserved.")
    typer.echo("Next: blitzecdn edge add NAME --host ADDRESS --ssh-source YOUR_CIDR")
