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
from blitzecdn.domain.models import (
    DeploymentStatus,
    DnsRecord,
    Domain,
    RecordType,
)
from blitzecdn.exceptions import BlitzeError
from blitzecdn.infrastructure.inventory import Inventory
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
site_app = typer.Typer(
    no_args_is_help=True,
    help="Inspect CDN virtual hosts (derived from proxied DNS records).",
)
edge_app = typer.Typer(no_args_is_help=True, help="Manage edge servers.")
domain_app = typer.Typer(no_args_is_help=True, help="Manage DNS zones.")
record_app = typer.Typer(no_args_is_help=True, help="Manage DNS records.")
dns_app = typer.Typer(no_args_is_help=True, help="Export DNS state.")
app.add_typer(site_app, name="site")
app.add_typer(edge_app, name="edge")
app.add_typer(domain_app, name="domain")
app.add_typer(record_app, name="record")
app.add_typer(dns_app, name="dns")


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
def setup() -> None:
    """Prepare local configuration and an empty edge inventory."""
    root = Path.cwd()
    environment_path = root / ".env"
    inventory_path = root / "ansible/inventory/hosts.yml"
    created: list[str] = []
    if not environment_path.exists():
        environment_path.write_text(
            f"BLITZE_API_KEYS=local:{secrets.token_urlsafe(48)}\n", encoding="utf-8"
        )
        environment_path.chmod(0o600)
        created.append(str(environment_path.relative_to(root)))
    if Inventory(inventory_path).initialize():
        created.append(str(inventory_path.relative_to(root)))
    if created:
        typer.echo(f"BlitzeCDN is ready. Created: {', '.join(created)}")
    else:
        typer.echo("BlitzeCDN is already set up; existing files were preserved.")
    typer.echo("Next: blitzecdn edge add NAME --host ADDRESS --ssh-source YOUR_CIDR")


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
    """Validate, preview, and apply desired state to every configured edge."""
    control = _control_plane()
    if not yes:
        errors = control.validate()
        if errors:
            _emit({"valid": False, "errors": errors}, json_output=json_output)
            raise typer.Exit(ExitCode.CONFIGURATION)
        typer.echo("Configuration is valid. Previewing changes...")
        preview = control.deploy("cli", check=True)
        if preview.status is not DeploymentStatus.SUCCEEDED:
            _emit(preview, json_output=json_output)
            raise typer.Exit(ExitCode.DEPLOYMENT_FAILED)
        if preview.stdout.strip():
            typer.echo(preview.stdout.rstrip())
        if not typer.confirm("Apply these changes to all configured edges?"):
            raise typer.Abort()
    result = control.deploy("cli")
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


@domain_app.command("add")
def domain_add(
    name: Annotated[str, typer.Argument(help="Zone to serve, e.g. example.com.")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Register a DNS zone delegated to BlitzeCDN."""
    _emit(
        _control_plane().create_domain(Domain(name=name), "cli"),
        json_output=json_output,
    )


@domain_app.command("list")
def domain_list(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    _emit(_control_plane().list_domains(), json_output=json_output)


@domain_app.command("import")
def domain_import(
    names: Annotated[
        list[str], typer.Argument(help="Every zone to import. List them all at once.")
    ],
    force: Annotated[
        bool,
        typer.Option("--force", help="Drop hostnames whose zone you did not list."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Convert sites created before v1.1.0 into proxied records.

    Sites are derived from records now. One created by an older release keeps
    working until the first record change rewrites the derived table, at which
    point it disappears.

    Pass every zone in a single command. Importing rewrites that derived table,
    which destroys the sites a second run would have read, so importing one zone
    at a time would lose the rest. The command refuses rather than let that
    happen.
    """
    result = _control_plane().import_sites(names, "cli", force=force)
    _emit(result, json_output=json_output)
    if json_output:
        return
    if result["skipped"]:
        typer.echo(
            "\nSome sites need attention before they are served again:", err=True
        )
        for problem in result["skipped"]:
            typer.echo(f"  - {problem}", err=True)
    if result["dropped"]:
        typer.echo(
            "\nThese hostnames are no longer in the desired state and will stop "
            "being served on the next deploy:",
            err=True,
        )
        for hostname in result["dropped"]:
            typer.echo(f"  - {hostname}", err=True)
        typer.echo(
            "\nImport every remaining zone before deploying. Run 'blitzecdn "
            "site list' to confirm the edge will serve what you expect.",
            err=True,
        )


@domain_app.command("remove")
def domain_remove(
    name: str,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Remove a zone and every record in it."""
    if not yes and not typer.confirm(f"Delete {name!r} and all of its records?"):
        raise typer.Abort()
    _control_plane().delete_domain(name, "cli")
    typer.echo(f"Deleted {name}")


@record_app.command("add")
def record_add(
    domain: Annotated[str, typer.Argument(help="Zone the record belongs to.")],
    name: Annotated[str, typer.Argument(help="Subdomain label, '@', or '*'.")],
    value: Annotated[str, typer.Option("--value", help="IP address to point at.")],
    type_: Annotated[RecordType, typer.Option("--type")] = RecordType.A,
    ttl: Annotated[int, typer.Option("--ttl")] = 300,
    proxied: Annotated[
        bool,
        typer.Option(
            "--proxied/--no-proxied",
            help="Serve through the CDN edge, or resolve straight to --value.",
        ),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Add a DNS record. Proxied records become an edge virtual host."""
    record = DnsRecord(
        domain=domain, name=name, type=type_, value=value, ttl=ttl, proxied=proxied
    )
    _emit(_control_plane().create_record(record, "cli"), json_output=json_output)


@record_app.command("list")
def record_list(
    domain: Annotated[str | None, typer.Argument()] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    _emit(_control_plane().list_records(domain), json_output=json_output)


@record_app.command("proxy")
def record_proxy(
    domain: Annotated[str, typer.Argument()],
    name: Annotated[str, typer.Argument()],
    on: Annotated[
        bool, typer.Option("--on/--off", help="Route through the CDN, or bypass it.")
    ],
    type_: Annotated[RecordType, typer.Option("--type")] = RecordType.A,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Turn the CDN on or off for one record.

    Takes effect on the edge at the next deploy. It only reaches clients once
    DNS answers accordingly, which the DNS system owns.
    """
    record = _control_plane().set_proxied(domain, name, type_, on, "cli")
    _emit(record, json_output=json_output)
    if not json_output:
        typer.echo(
            f"{record.fqdn} is now "
            f"{'proxied through the CDN' if on else 'bypassing the CDN'}. "
            "Run 'blitzecdn deploy' to apply, and make sure DNS points at "
            f"{'an edge' if on else record.value}."
        )


@record_app.command("remove")
def record_remove(
    domain: Annotated[str, typer.Argument()],
    name: Annotated[str, typer.Argument()],
    type_: Annotated[RecordType, typer.Option("--type")] = RecordType.A,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    label = f"{name}.{domain}" if name != "@" else domain
    if not yes and not typer.confirm(f"Delete {type_.value} record for {label!r}?"):
        raise typer.Abort()
    _control_plane().delete_record(domain, name, type_, "cli")
    typer.echo(f"Deleted {label}")


@dns_app.command("export")
def dns_export(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Emit every record for the system that publishes DNS.

    Proxied records carry no address: they must resolve to an edge, and edge
    addressing is owned by the DNS system rather than the control plane.
    """
    _emit(_control_plane().dns_export(), json_output=json_output)


@edge_app.command("list")
def edge_list(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List configured edge servers."""
    settings = _settings()
    _emit(Inventory(settings.inventory_path).list_edges(), json_output=json_output)


@edge_app.command("add")
def edge_add(
    name: Annotated[str, typer.Argument(help="Stable edge name.")],
    host: Annotated[str, typer.Option("--host", help="SSH hostname or address.")],
    ssh_source: Annotated[
        list[str],
        typer.Option(
            "--ssh-source",
            help="Trusted management CIDR; repeat the option to add more.",
        ),
    ],
    user: Annotated[str, typer.Option("--user", help="Non-root SSH user.")] = "deploy",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Add an edge while preserving fail-closed firewall policy."""
    if not ssh_source:
        raise typer.BadParameter(
            "at least one --ssh-source management CIDR is required"
        )
    settings = _settings()
    edge = Inventory(settings.inventory_path).add_edge(
        name, host=host, user=user, ssh_sources=ssh_source
    )
    _emit(edge, json_output=json_output)


@edge_app.command("remove")
def edge_remove(
    name: str,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Remove an edge from desired state."""
    if not yes and not typer.confirm(f"Stop managing edge {name!r}?"):
        raise typer.Abort()
    Inventory(_settings().inventory_path).remove_edge(name)
    typer.echo(f"Removed {name}")


def run() -> None:
    try:
        app()
    except (BlitzeError, ValidationError, OSError) as exc:
        typer.echo(f"error: {exc}", err=True)
        # SystemExit, not typer.Exit: we are outside Click's invocation by the
        # time app() has raised, so a typer.Exit here is nothing but an
        # unhandled exception and prints a traceback over the message above.
        raise SystemExit(ExitCode.INVALID_INPUT) from exc


if __name__ == "__main__":
    run()
