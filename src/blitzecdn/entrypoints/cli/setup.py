"""First-run commands: scaffold the local configuration a controller needs.

No inventory file: Ansible reads the fleet through the inventory plugin.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Annotated

import typer

from blitzecdn.composition import migrate_database
from blitzecdn.entrypoints.cli import common
from blitzecdn.entrypoints.cli.root import app


def _environment_file_body() -> str:
    """The local API credential; non-secret settings belong in TOML."""
    return f"BLITZE_API_KEYS=local:{secrets.token_urlsafe(48)}\n"


@app.command()
def init(
    output: Annotated[Path, typer.Option(help="Environment file to create.")] = Path(
        ".env"
    ),
) -> None:
    """Create a restrictive local environment file without overwriting one."""
    if output.exists():
        raise typer.BadParameter(f"refusing to overwrite {output}")
    output.write_text(_environment_file_body(), encoding="utf-8")
    output.chmod(0o600)
    typer.echo(f"Created {output} with mode 0600")


@app.command()
def migrate() -> None:
    """Initialize or upgrade the database while controller services are stopped."""
    migrate_database(common.settings())
    typer.echo("Database schema is current.")


@app.command()
def setup() -> None:
    """Initialize the database and scaffold local configuration; safe to re-run."""
    migrate_database(common.settings())
    root = Path.cwd()
    environment_path = root / ".env"
    created: list[str] = []
    if not environment_path.exists():
        environment_path.write_text(_environment_file_body(), encoding="utf-8")
        environment_path.chmod(0o600)
        created.append(str(environment_path.relative_to(root)))
    if created:
        typer.echo(f"BlitzeCDN is ready. Created: {', '.join(created)}")
    else:
        typer.echo("BlitzeCDN is already set up; existing files were preserved.")
    typer.echo("Next: blitzecdn edge add NAME --host ADDRESS --ssh-source YOUR_CIDR")
