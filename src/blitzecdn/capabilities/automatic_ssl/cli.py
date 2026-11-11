"""`ssl` — run Automatic SSL/TLS scans."""

from __future__ import annotations

import typer
from dishka import FromDishka

from blitzecdn.capabilities.automatic_ssl.service import AutomaticSslService
from blitzecdn.capabilities.deployments import DeploymentStatus
from blitzecdn.delivery.cli import common
from blitzecdn.delivery.cli.common import ExitCode

ssl_app = typer.Typer(
    no_args_is_help=True,
    help="Scan origins and apply upgrade-only Automatic SSL/TLS recommendations.",
)


@ssl_app.command("reconcile")
@common.inject
def reconcile(
    automatic_ssl: FromDishka[AutomaticSslService],
    json_output: common.JsonOutput = False,
) -> None:
    """Run the same Automatic SSL/TLS scan as the monthly scheduler."""
    result = automatic_ssl.reconcile("cli")
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
