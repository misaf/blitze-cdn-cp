"""Convergence commands: validate, plan, deploy, rollback, drift, status."""

from __future__ import annotations

from typing import Annotated

import typer

from blitzecdn.application.commands import (
    CheckDriftCommand,
    DeployCommand,
    ReconcileCertificatesCommand,
    RollbackCommand,
    ValidateCommand,
)
from blitzecdn.cli import common
from blitzecdn.cli.app import app
from blitzecdn.cli.common import ExitCode
from blitzecdn.domain.deployments import DeploymentStatus


@app.command()
def validate(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Validate configuration, desired state, inventory, and playbook syntax."""
    errors = ValidateCommand().execute(common.control_plane(), "cli")
    common.emit({"valid": not errors, "errors": errors}, json_output=json_output)
    if errors:
        raise typer.Exit(ExitCode.CONFIGURATION)


@app.command()
def plan(
    limit: Annotated[str | None, common.LIMIT_OPTION] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run Ansible check mode and show the resulting deployment record."""
    result = DeployCommand(check=True, host_limit=limit).execute(
        common.control_plane(), "cli"
    )
    common.emit(result, json_output=json_output)
    if result.status is not DeploymentStatus.SUCCEEDED:
        raise typer.Exit(ExitCode.DEPLOYMENT_FAILED)


@app.command()
def deploy(
    yes: Annotated[
        bool,
        typer.Option(
            "--yes", help="Required confirmation for non-interactive execution."
        ),
    ] = False,
    limit: Annotated[str | None, common.LIMIT_OPTION] = None,
    request_certificates: Annotated[
        bool,
        typer.Option(
            "--request-certificates",
            help=(
                "After the HTTP deployment, issue certificates for ready "
                "proxied sites and deploy again to install them."
            ),
        ),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Validate, preview, and apply desired state to the configured edges.

    With --limit this is a canary: the named edges converge and the rest keep
    serving what they have. Re-run without it to finish the rollout.
    """
    if request_certificates and limit:
        raise typer.BadParameter(
            "--request-certificates cannot be combined with --limit; HTTP-01 "
            "must be deployed to the full edge fleet"
        )
    control = common.control_plane()
    if not yes:
        errors = ValidateCommand().execute(control, "cli")
        if errors:
            common.emit({"valid": False, "errors": errors}, json_output=json_output)
            raise typer.Exit(ExitCode.CONFIGURATION)
        typer.echo("Configuration is valid. Previewing changes...")
        preview = DeployCommand(check=True, host_limit=limit).execute(control, "cli")
        if preview.status is not DeploymentStatus.SUCCEEDED:
            common.emit(preview, json_output=json_output)
            raise typer.Exit(ExitCode.DEPLOYMENT_FAILED)
        rendered = common.describe_hosts(preview.hosts)
        typer.echo(rendered or "  No edge reported a result.")
        target = f"edges matching {limit!r}" if limit else "all configured edges"
        if not typer.confirm(f"Apply these changes to {target}?"):
            raise typer.Abort()
    result = DeployCommand(host_limit=limit).execute(control, "cli")
    if result.status is not DeploymentStatus.SUCCEEDED:
        common.emit(result, json_output=json_output)
        raise typer.Exit(ExitCode.DEPLOYMENT_FAILED)

    if request_certificates:
        # The same reconciliation the scheduler and `blitzecdn cert reconcile`
        # run, rather than a second copy of the loop. The copy that used to
        # live here lacked both of the things that make the real one safe: it
        # never re-checked eligibility under the deployment lock, so two
        # reconcilers could each contact the CA for one site, and one failing
        # site aborted the rest instead of being recorded and stepped over.
        reconciliation = ReconcileCertificatesCommand().execute(control, "cli")
        common.emit(
            {
                "deployment": result.model_dump(mode="json"),
                "certificates": reconciliation.model_dump(mode="json"),
            },
            json_output=json_output,
        )
        certificate_deployment = reconciliation.deployment
        if reconciliation.failed or (
            certificate_deployment is not None
            and certificate_deployment.status is not DeploymentStatus.SUCCEEDED
        ):
            raise typer.Exit(ExitCode.DEPLOYMENT_FAILED)
    else:
        common.emit(result, json_output=json_output)
    if not json_output and limit:
        typer.echo(
            f"\nThis was a canary against {limit!r}. Every other edge is still "
            "serving its previous configuration; re-run 'blitzecdn deploy' "
            "without --limit to finish the rollout."
        )


@app.command()
def rollback(
    deployment_id: Annotated[
        str | None,
        typer.Argument(
            help="Successful deployment ID; defaults to the latest different state."
        ),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes")] = False,
    check: Annotated[
        bool, typer.Option("--check", help="Preview without changing canonical state.")
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Converge a prior successful snapshot, then make it canonical on success."""
    if (
        not check
        and not yes
        and not typer.confirm(
            "Rollback edge configuration and canonical desired state?"
        )
    ):
        raise typer.Abort()
    result = RollbackCommand(deployment_id=deployment_id, check=check).execute(
        common.control_plane(), "cli"
    )
    common.emit(result, json_output=json_output)
    if result.status is not DeploymentStatus.SUCCEEDED:
        raise typer.Exit(ExitCode.DEPLOYMENT_FAILED)


@app.command()
def drift(
    limit: Annotated[str | None, common.LIMIT_OPTION] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Report whether the edges still match the declared desired state.

    Changes nothing: it is a check-mode run read as a question. Exits 6 when a
    reachable edge would change, so it can be scheduled and alerted on.
    """
    report = CheckDriftCommand(host_limit=limit).execute(common.control_plane(), "cli")
    common.emit(
        {
            "deployment_id": report.deployment_id,
            "checked_at": report.checked_at.isoformat(),
            "in_sync": report.in_sync,
            "hosts": [host.model_dump(mode="json") for host in report.hosts],
        },
        json_output=json_output,
    )
    if json_output:
        pass
    elif not report.hosts:
        typer.echo(
            "\nNo edge reported a result. Check that the inventory has hosts "
            "and that the run above completed.",
            err=True,
        )
    elif report.in_sync:
        typer.echo(f"\nAll {len(report.hosts)} edges match desired state.")
    else:
        for host in report.drifted:
            typer.echo(
                f"\n{host.host} would change {host.changed} task(s). "
                "Run 'blitzecdn deploy' to converge it.",
                err=True,
            )
        for host in report.unreachable:
            typer.echo(
                f"\n{host.host} could not be reached, so nothing is known about "
                "its configuration.",
                err=True,
            )
    if not report.in_sync:
        raise typer.Exit(ExitCode.DRIFT_DETECTED)


@app.command()
def status(
    deployment_id: Annotated[str | None, typer.Argument()] = None,
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show one deployment or recent deployment history."""
    deployments = common.control_plane().deployments
    value = (
        deployments.get_deployment(deployment_id)
        if deployment_id
        else deployments.list_deployments(limit)
    )
    common.emit(value, json_output=json_output)
