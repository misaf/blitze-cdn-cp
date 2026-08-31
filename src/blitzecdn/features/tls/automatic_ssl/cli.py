"""`ssl` — run Cloudflare-style Automatic SSL/TLS scans.

Beside the scan it drives, not beside the certificate commands. It was
`certificates/tls_cli.py`: an Automatic SSL/TLS entry point in the package that
issues material, reaching `platform.automatic_ssl` from a module that owns
none of it.
"""

from __future__ import annotations

from typing import Annotated

import typer

from blitzecdn.cli import common
from blitzecdn.cli.common import ExitCode
from blitzecdn.features.deployments.domain import DeploymentStatus

ssl_app = typer.Typer(
    no_args_is_help=True,
    help="Scan origins and apply upgrade-only Automatic SSL/TLS recommendations.",
)


@ssl_app.command("reconcile")
def reconcile(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run the same Automatic SSL/TLS scan as the monthly scheduler."""
    result = common.control_plane().automatic_ssl.reconcile("cli")
    common.emit(result, json_output=json_output)
    if not json_output:
        if result.upgraded:
            typer.echo(
                "Upgraded "
                + ", ".join(
                    f"{site} to {mode.value}" for site, mode in result.upgraded.items()
                )
                + "."
            )
        elif not result.scanned:
            typer.echo("No sites are eligible for Automatic SSL/TLS scanning.")
        else:
            typer.echo("No stronger compatible SSL mode was found.")
    if (
        result.deployment is not None
        and result.deployment.status is not DeploymentStatus.SUCCEEDED
    ):
        raise typer.Exit(ExitCode.DEPLOYMENT_FAILED)
