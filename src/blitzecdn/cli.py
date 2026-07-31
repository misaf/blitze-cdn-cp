from __future__ import annotations

import json
import secrets
from enum import IntEnum
from pathlib import Path
from typing import Annotated, Any

import typer
import uvicorn
import yaml
from pydantic import ValidationError

from blitzecdn.api import create_app
from blitzecdn.application import ControlPlane
from blitzecdn.config import Settings
from blitzecdn.domain.models import CdnSite, DeploymentStatus
from blitzecdn.exceptions import BlitzeError
from blitzecdn.logging import configure_logging


class ExitCode(IntEnum):
    OK = 0
    INVALID_INPUT = 2
    CONFIGURATION = 3
    CONFLICT = 4
    DEPLOYMENT_FAILED = 5


app = typer.Typer(
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    help="Securely manage BlitzeCDN edge desired state.",
)
site_app = typer.Typer(no_args_is_help=True, help="Manage CDN sites.")
app.add_typer(site_app, name="site")


def _settings() -> Settings:
    return Settings.from_environment()


def _control_plane() -> ControlPlane:
    return ControlPlane(_settings())


def _emit(value: Any, *, json_output: bool) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    elif isinstance(value, list):
        value = [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else item
            for item in value
        ]
    if json_output:
        typer.echo(json.dumps(value, indent=2, sort_keys=True))
    elif isinstance(value, (dict, list)):
        typer.echo(yaml.safe_dump(value, sort_keys=False).rstrip())
    else:
        typer.echo(str(value))


@app.callback()
def main(
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Enable diagnostic logging.")
    ] = False,
    log_json: Annotated[bool, typer.Option(help="Write logs as JSON.")] = False,
) -> None:
    configure_logging(verbose=verbose, json_output=log_json)


@app.command()
def init(
    output: Annotated[Path, typer.Option(help="Environment file to create.")] = Path(
        ".env"
    ),
) -> None:
    """Create a restrictive local environment file without overwriting one."""
    if output.exists():
        raise typer.BadParameter(f"refusing to overwrite {output}")
    output.write_text(
        f"BLITZE_API_KEYS=local:{secrets.token_urlsafe(48)}\n", encoding="utf-8"
    )
    output.chmod(0o600)
    typer.echo(f"Created {output} with mode 0600")


@app.command()
def validate(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Validate configuration, desired state, inventory, and playbook syntax."""
    errors = _control_plane().validate()
    _emit({"valid": not errors, "errors": errors}, json_output=json_output)
    if errors:
        raise typer.Exit(ExitCode.CONFIGURATION)


@app.command()
def plan(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Run Ansible check mode and show the resulting deployment record."""
    result = _control_plane().deploy("cli", check=True)
    _emit(result, json_output=json_output)
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
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Apply current desired state to every configured edge."""
    if not yes and not typer.confirm(
        "Apply desired state to all blitzecdn_edges hosts?"
    ):
        raise typer.Abort()
    result = _control_plane().deploy("cli")
    _emit(result, json_output=json_output)
    if result.status is not DeploymentStatus.SUCCEEDED:
        raise typer.Exit(ExitCode.DEPLOYMENT_FAILED)


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
    result = _control_plane().rollback("cli", deployment_id, check=check)
    _emit(result, json_output=json_output)
    if result.status is not DeploymentStatus.SUCCEEDED:
        raise typer.Exit(ExitCode.DEPLOYMENT_FAILED)


@app.command()
def status(
    deployment_id: Annotated[str | None, typer.Argument()] = None,
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show one deployment or recent deployment history."""
    repository = _control_plane().repository
    value = (
        repository.get_deployment(deployment_id)
        if deployment_id
        else repository.list_deployments(limit)
    )
    _emit(value, json_output=json_output)


@app.command()
def audit(
    limit: Annotated[int, typer.Option(min=1, max=500)] = 100,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show immutable operator audit events."""
    _emit(_control_plane().repository.list_audit_events(limit), json_output=json_output)


@app.command()
def doctor(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Report local readiness without contacting remote servers."""
    settings = _settings()
    report = {
        "python_supported": True,
        "state_dir": str(settings.state_dir),
        "api_auth_configured": bool(settings.api_keys),
        "configuration_errors": settings.validate_runtime(),
    }
    _emit(report, json_output=json_output)
    if report["configuration_errors"]:
        raise typer.Exit(ExitCode.CONFIGURATION)


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8000,
) -> None:
    """Run the authenticated HTTP control plane."""
    settings = _settings()
    if not settings.api_keys:
        raise typer.BadParameter("configure BLITZE_API_KEYS before starting the API")
    uvicorn.run(create_app(settings), host=host, port=port, access_log=True)


@site_app.command("list")
def site_list(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    _emit(_control_plane().repository.list_sites(), json_output=json_output)


@site_app.command("add")
def site_add(
    file: Annotated[
        Path, typer.Option("--file", exists=True, dir_okay=False, readable=True)
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Add a site from a validated YAML or JSON document."""
    payload = yaml.safe_load(file.read_text(encoding="utf-8"))
    site = CdnSite.model_validate(payload)
    _emit(_control_plane().create_site(site, "cli"), json_output=json_output)


@site_app.command("remove")
def site_remove(
    name: str,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    if not yes and not typer.confirm(f"Delete desired state for {name!r}?"):
        raise typer.Abort()
    _control_plane().delete_site(name, "cli")
    typer.echo(f"Deleted {name}")


def run() -> None:
    try:
        app()
    except (BlitzeError, ValidationError, OSError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(ExitCode.INVALID_INPUT) from exc


if __name__ == "__main__":
    run()
