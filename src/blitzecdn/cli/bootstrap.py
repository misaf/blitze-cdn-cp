"""First-run commands: scaffold the local configuration a controller needs.

``setup`` creates no inventory file. The fleet lives in the ``edges`` table and
Ansible reads it through the ``blitzecdn`` inventory plugin, so the only
inventory artefact is the plugin's configuration — which is tracked and ships
with the project.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Annotated

import typer

from blitzecdn.cli import common
from blitzecdn.cli.app import app


def _environment_file_body() -> str:
    """The scaffolded `.env`, with the optional settings named but unset.

    Commented rather than omitted: an operator who never reads the reference
    should still be able to discover that these exist, and a key that has to be
    exported into the shell before each deploy gets exported once and then
    forgotten. `.env` is written 0600 and is the intended home for both.
    """
    return (
        f"BLITZE_API_KEYS=local:{secrets.token_urlsafe(48)}\n"
        "\n"
        "# MaxMind GeoLite2, for per-hostname country filtering. Free, but the\n"
        "# download is authenticated: create an account, generate a license key,\n"
        "# and set blitzecdn_edge_geoip_enabled in group_vars. The license key\n"
        "# is an account credential — this file is 0600 and must stay uncommitted.\n"
        "# BLITZE_MAXMIND_ACCOUNT_ID=\n"
        "# BLITZE_MAXMIND_LICENSE_KEY=\n"
    )


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
def setup(
    schema_only: Annotated[
        bool,
        typer.Option(
            "--schema-only",
            hidden=True,
            help="Initialize the database without scaffolding local files.",
        ),
    ] = False,
) -> None:
    """Prepare the local configuration a controller needs to run.

    Safe to re-run: nothing that already exists is overwritten.
    """
    # Create the database on first run. Ansible reads the fleet
    # through the `blitzecdn` inventory plugin, which refuses a database that
    # does not exist — so on a fresh install every playbook failed to parse its
    # inventory until something happened to create one. `setup` is the command
    # whose whole job is that something.
    control = common.control_plane()
    control.close()
    if schema_only:
        return
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
