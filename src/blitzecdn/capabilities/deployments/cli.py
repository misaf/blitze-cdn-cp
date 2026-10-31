"""Convergence verbs and deployment history queries."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Annotated

import typer

from blitzecdn.capabilities.deployments.domain import DeploymentStatus
from blitzecdn.capabilities.deployments.service.convergence import DeploymentService
from blitzecdn.core.exceptions import DeploymentBusyError
from blitzecdn.entrypoints.cli import common
from blitzecdn.entrypoints.cli.common import ExitCode

#: Root-level verbs: `blitzecdn deploy`, not `blitzecdn deployment deploy`.
deployment_app = typer.Typer()

deployment_queries_app = typer.Typer(
    no_args_is_help=True, help="Inspect deployment history and per-edge progress."
)

Wait = Annotated[
    int,
    typer.Option(
        "--wait",
        min=0,
        max=3600,
        help=(
            "When another run holds the deployment lock, keep trying for up to "
            "this many seconds instead of failing at once."
        ),
    ),
]

_WAIT_POLL_SECONDS = 2.0


def _when_free[T](wait: int, attempt: Callable[[], T]) -> T:
    """Run ``attempt``, retrying for ``wait`` seconds while the lock is busy.

    Safe to repeat: each service call takes the lock before it writes anything,
    so an attempt refused as busy left nothing behind.
    """
    deadline = time.monotonic() + wait
    while True:
        try:
            return attempt()
        except DeploymentBusyError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(_WAIT_POLL_SECONDS, remaining))


@deployment_app.command()
def validate(json_output: common.JsonOutput = False) -> None:
    """Validate configuration, desired state, inventory, and playbook syntax."""
    errors = common.service(DeploymentService).validate()
    common.emit({"valid": not errors, "errors": errors}, json_output=json_output)
    if errors:
        raise typer.Exit(ExitCode.CONFIGURATION)


@deployment_app.command()
def plan(
    limit: Annotated[str | None, common.LIMIT_OPTION] = None,
    wait: Wait = 0,
    json_output: common.JsonOutput = False,
) -> None:
    """Run Ansible check mode and show the resulting deployment record."""
    deployments = common.service(DeploymentService)
    result = _when_free(
        wait, lambda: deployments.deploy("cli", check=True, host_limit=limit)
    )
    common.emit(result, json_output=json_output)
    if result.status is not DeploymentStatus.SUCCEEDED:
        raise typer.Exit(ExitCode.DEPLOYMENT_FAILED)


@deployment_app.command()
def deploy(
    yes: common.Yes = False,
    skip_preflight: common.SkipPreflight = False,
    limit: Annotated[str | None, common.LIMIT_OPTION] = None,
    wait: Wait = 0,
    json_output: common.JsonOutput = False,
) -> None:
    """Validate, preview, and apply desired state to the configured edges.

    With --limit this is a canary: the named edges converge and the rest keep
    serving. --yes answers only the confirmation; skipping the checks is
    --skip-preflight.
    """
    deployments = common.service(DeploymentService)
    if not skip_preflight:
        errors = deployments.validate()
        if errors:
            common.emit({"valid": False, "errors": errors}, json_output=json_output)
            raise typer.Exit(ExitCode.CONFIGURATION)
        # Under --json this line would be the one non-JSON byte on stdout.
        if not json_output:
            typer.echo("Configuration is valid. Previewing changes...")
        preview = _when_free(
            wait, lambda: deployments.deploy("cli", check=True, host_limit=limit)
        )
        if preview.status is not DeploymentStatus.SUCCEEDED:
            common.emit(preview, json_output=json_output)
            raise typer.Exit(ExitCode.DEPLOYMENT_FAILED)
        if not json_output:
            rendered = common.describe_hosts(preview.hosts)
            typer.echo(rendered or "  No edge reported a result.")
    if not yes:
        target = f"edges matching {limit!r}" if limit else "all configured edges"
        if not typer.confirm(f"Apply these changes to {target}?"):
            raise typer.Abort()
    result = _when_free(wait, lambda: deployments.deploy("cli", host_limit=limit))
    if result.status is not DeploymentStatus.SUCCEEDED:
        common.emit(result, json_output=json_output)
        raise typer.Exit(ExitCode.DEPLOYMENT_FAILED)

    common.emit(result, json_output=json_output)
    if not json_output and limit:
        typer.echo(
            f"\nThis was a canary against {limit!r}. Every other edge is still "
            "serving its previous configuration; re-run 'blitzecdn deploy' "
            "without --limit to finish the rollout."
        )


@deployment_app.command()
def rollback(
    deployment_id: Annotated[
        str | None,
        typer.Argument(
            help="Successful deployment ID; defaults to the latest different state."
        ),
    ] = None,
    yes: common.Yes = False,
    check: Annotated[
        bool, typer.Option("--check", help="Preview without changing canonical state.")
    ] = False,
    json_output: common.JsonOutput = False,
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
    result = common.service(DeploymentService).rollback(
        "cli", deployment_id, check=check
    )
    common.emit(result, json_output=json_output)
    if result.status is not DeploymentStatus.SUCCEEDED:
        raise typer.Exit(ExitCode.DEPLOYMENT_FAILED)


@deployment_app.command()
def drift(
    limit: Annotated[str | None, common.LIMIT_OPTION] = None,
    wait: Wait = 0,
    json_output: common.JsonOutput = False,
) -> None:
    """Report whether the edges still match the declared desired state.

    A check-mode run that changes nothing. Exits 6 when a reachable edge would
    change, so it can be scheduled and alerted on.
    """
    deployments = common.service(DeploymentService)
    report = _when_free(wait, lambda: deployments.check_drift("cli", host_limit=limit))
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


@deployment_queries_app.command()
def status(
    deployment_id: Annotated[str | None, typer.Argument()] = None,
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
    json_output: common.JsonOutput = False,
) -> None:
    """Show one deployment, with how far each edge got, or recent history."""
    deployments = common.service(DeploymentService)
    if not deployment_id:
        common.emit(deployments.list_deployments(limit), json_output=json_output)
        return
    deployment = deployments.get_deployment(deployment_id)
    common.emit(deployment, json_output=json_output)
    targets = [
        {
            "edge": target.edge,
            "status": target.status.value,
            "phase": target.phase.value if target.phase else None,
            "attempts": target.attempts,
            "last_error": target.last_error,
        }
        for target in deployments.targets(deployment.id)
    ]
    if targets:
        common.emit(targets, json_output=json_output)
