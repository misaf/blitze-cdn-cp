"""Disaster recovery: take a backup, look inside one, put one back."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from blitzecdn.cli import common
from blitzecdn.control_plane import build_backup_service
from blitzecdn.features.backup.domain import BackupComponent

backup_app = typer.Typer(
    no_args_is_help=True,
    help="""Back up and restore the control plane's authoritative state.

    'create' with no options takes everything a rebuilt controller would
    otherwise have lost: the database, the certificates and their keys, the
    ACME account, and the configuration. '--only' narrows that; the manifest
    inside the archive records what was taken, and 'restore' puts back exactly
    what the manifest lists.

    Archives hold private keys and the whole audit trail. They are written
    0600 into a 0700 directory, and copying them off the server is the point —
    a backup that only exists on the host it protects is not one.
    """,
)

_ONLY = typer.Option(
    "--only",
    help=(
        "Back up exactly this component; repeat for more than one: "
        + ", ".join(member.value for member in BackupComponent)
        + ". Omit for a full disaster-recovery backup."
    ),
)


@backup_app.command("create")
def create(
    only: Annotated[list[BackupComponent] | None, _ONLY] = None,
    destination: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Write the archive here instead of the backup directory.",
        ),
    ] = None,
) -> None:
    """Write one archive of the state this controller cannot regenerate."""
    service = build_backup_service(common.settings())
    created = service.create(only=only, destination=destination)
    typer.echo(f"Backup created: {created}")


@backup_app.command("inspect")
def inspect(
    archive: Annotated[Path, typer.Argument(help="The archive to describe.")],
) -> None:
    """Show what an archive holds, without extracting or restoring any of it."""
    manifest = build_backup_service(common.settings()).inspect(archive)
    typer.echo(f"Created: {manifest.created_at:%Y-%m-%d %H:%M:%S} UTC")
    typer.echo(f"BlitzeCDN version: {manifest.blitzecdn_version}")
    typer.echo(f"Backup format: {manifest.format_version}")
    if manifest.database_schema_version:
        typer.echo(f"Database schema: {manifest.database_schema_version}")
    typer.echo("Components:")
    for component in manifest.components:
        typer.echo(f"  {component.value}")


@backup_app.command("restore")
def restore(
    archive: Annotated[Path, typer.Argument(help="The archive to restore from.")],
    assume_yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")
    ] = False,
) -> None:
    """Restore the components the archive's manifest declares, and nothing else.

    There is no `--only` here on purpose: the archive already records what it
    holds, and asking an operator to repeat the selection is an invitation to
    name a component the archive does not contain.
    """
    service = build_backup_service(common.settings())
    manifest = service.inspect(archive)
    listed = ", ".join(component.value for component in manifest.components)
    if not assume_yes:
        typer.confirm(
            f"Restore {listed} from {archive}, replacing what is there now?",
            abort=True,
        )
    service.restore(archive)
    typer.echo(f"Restored: {listed}")
    if BackupComponent.DATABASE in manifest.components:
        # Edge configuration is generated, never archived, so the fleet is
        # still converged to whatever the previous desired state was.
        typer.echo("Run 'blitzecdn deploy' to converge the edges on restored state.")
