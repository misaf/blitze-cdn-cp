"""`cert` — inspect, preflight, renew and reconcile managed TLS certificates."""

from __future__ import annotations

from typing import Annotated, Any

import typer

from blitzecdn.cli import common
from blitzecdn.cli.common import ExitCode
from blitzecdn.features.deployments.domain import DeploymentStatus
from blitzecdn.features.tls.certificates.domain import CERTIFICATE_RENEWAL_DAYS

cert_app = typer.Typer(
    no_args_is_help=True, help="Inspect and renew managed TLS certificates."
)


@cert_app.command("list")
def cert_list(
    expiring_in: Annotated[
        int | None,
        typer.Option(
            "--expiring-in",
            help="Show only certificates with at most this many days left.",
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List managed certificates, soonest expiry first.

    Exits 4 if any listed certificate has already expired, so a scheduled
    check notices without anyone reading the output.
    """
    control = common.control_plane()
    statuses = (
        control.certificates.expiring_certificates(expiring_in)
        if expiring_in is not None
        else control.certificates.certificate_statuses()
    )
    common.emit(statuses, json_output=json_output)
    if not json_output and not statuses:
        typer.echo("No managed certificates.")
    if any(status.expired for status in statuses):
        raise typer.Exit(ExitCode.CONFLICT)


@cert_app.command("preflight")
def cert_preflight(
    name: Annotated[str, typer.Argument(help="Site name.")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Check whether a certificate could be issued for a site right now.

    Looks at what issuance actually depends on and the control plane cannot
    control: that the hostname resolves to one of our edges, that CAA permits
    our CA, that the vhost is already deployed, plus advisories on the origin
    and the record's TTL. Contacts no CA and changes nothing, so it is safe to
    run repeatedly while a customer is still moving their DNS.

    Exits 3 if anything blocks issuance. Advisories alone do not.
    """
    report = common.control_plane().certificates.certificate_preflight(name)
    common.emit(report, json_output=json_output)
    if not json_output:
        for check in report.checks:
            mark = "ok  " if check.passed else f"{check.severity.value.upper()}"
            typer.echo(f"  {mark:9} {check.name}: {check.detail}")
        if report.ok:
            typer.echo(f"\n{name} is ready for issuance.")
        else:
            typer.echo(f"\n{name} cannot be issued yet: {report.summary()}", err=True)
    if not report.ok:
        raise typer.Exit(ExitCode.CONFIGURATION)


@cert_app.command("renew")
def cert_renew(
    expiring_in: Annotated[
        int,
        typer.Option(
            "--expiring-in", help="Renew certificates with at most this many days left."
        ),
    ] = CERTIFICATE_RENEWAL_DAYS,
    force: Annotated[
        bool,
        typer.Option("--force", help="Renew every ACME certificate regardless of age."),
    ] = False,
    site: Annotated[
        list[str] | None,
        typer.Option(
            "--site",
            help=(
                "Renew only this site; repeat the option to add more. "
                "Without it every managed certificate is considered."
            ),
        ),
    ] = None,
    deploy_after: Annotated[
        bool,
        typer.Option(
            "--deploy",
            help="Deploy once after successful renewals so edges receive them.",
        ),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Reissue ACME certificates that are close to expiry.

    Safe to run on a schedule: a certificate that is not yet due is left
    alone, and one site failing does not stop the others. Renewed
    certificates reach the edges on the next deploy.

    Use --site to retry a single failure without sending every other
    subscription back to the CA, which is rate limited.
    """
    control = common.control_plane()
    result = control.certificates.renew_certificates(
        "cli",
        within_days=expiring_in,
        force=force,
        sites=site or None,
        budget_seconds=None,
    )
    deployment = None
    if deploy_after and result.renewed and not result.failed:
        deployment = control.deployments.deploy("cli")
    output: dict[str, Any] = result.model_dump(mode="json")
    if deploy_after:
        output["deployment"] = (
            deployment.model_dump(mode="json") if deployment is not None else None
        )
    common.emit(output, json_output=json_output)
    # Only the prose summary is suppressed under --json. The exit code below
    # still has to fire: --json is the scheduled-run path, and that is exactly
    # the caller that has nothing but the exit code to alert on.
    if not json_output:
        if result.renewed and not deploy_after:
            typer.echo(
                f"\nRenewed {len(result.renewed)} certificate(s). Run "
                "'blitzecdn deploy' to install them on the edges."
            )
        for problem in result.skipped:
            typer.echo(f"  - {problem}", err=True)
        for problem in result.failed:
            typer.echo(f"  - renewal failed: {problem}", err=True)
    if result.failed or (
        deployment is not None and deployment.status is not DeploymentStatus.SUCCEEDED
    ):
        raise typer.Exit(ExitCode.DEPLOYMENT_FAILED)


@cert_app.command("reconcile")
def cert_reconcile(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Issue ready first certificates and deploy them to the edge fleet.

    Safe to schedule frequently: sites with certificates are ignored, blocked
    preflights never contact the CA, and the deployment runs only after at
    least one new certificate was issued.
    """
    result = common.control_plane().certificates.reconcile_certificates("cli")
    common.emit(result, json_output=json_output)
    deployment = result.deployment
    if result.failed or (
        deployment is not None and deployment.status is not DeploymentStatus.SUCCEEDED
    ):
        raise typer.Exit(ExitCode.DEPLOYMENT_FAILED)
