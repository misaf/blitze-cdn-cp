"""`edge` and `origin` — the fleet itself, and what it proxies to."""

from __future__ import annotations

from typing import Annotated

import typer

from blitzecdn.application.commands import CheckOriginsCommand, DecommissionEdgeCommand
from blitzecdn.cli import common
from blitzecdn.cli.common import ExitCode
from blitzecdn.infrastructure.inventory import Inventory

edge_app = typer.Typer(no_args_is_help=True, help="Manage edge servers.")
origin_app = typer.Typer(
    no_args_is_help=True, help="Check the origins the edges proxy to."
)


@origin_app.command("check")
def origin_check(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Connect to every enabled site's origin the way the edge will.

    Catches the misconfigurations `validate` cannot see — an origin that does
    not resolve, is not listening, or presents a certificate that does not
    match the SNI the edge will send. Exits 3 if any origin fails.

    This runs from the controller, not from an edge, so a pass is good evidence
    rather than proof. A failure is almost always real.
    """
    results = CheckOriginsCommand().execute(common.control_plane(), "cli")
    common.emit(results, json_output=json_output)
    failures = [result for result in results if not result.ok]
    if not json_output:
        if not results:
            typer.echo("No enabled sites to check.")
        elif not failures:
            typer.echo(f"\nAll {len(results)} origins answered as expected.")
        for failure in failures:
            typer.echo(
                f"\n{failure.site} ({failure.origin}): {failure.detail}", err=True
            )
    if failures:
        raise typer.Exit(ExitCode.CONFIGURATION)


@edge_app.command("list")
def edge_list(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List configured edge servers."""
    settings = common.settings()
    common.emit(
        Inventory(settings.inventory_path).list_edges(), json_output=json_output
    )


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
    public_address: Annotated[
        list[str] | None,
        typer.Option(
            "--public-address",
            help=(
                "Public IP or hostname serving CDN traffic; repeat for NAT or "
                "multi-address edges. Defaults to --host."
            ),
        ),
    ] = None,
    user: Annotated[str, typer.Option("--user", help="Non-root SSH user.")] = "deploy",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Add an edge while preserving fail-closed firewall policy."""
    if not ssh_source:
        raise typer.BadParameter(
            "at least one --ssh-source management CIDR is required"
        )
    settings = common.settings()
    edge = Inventory(settings.inventory_path).add_edge(
        name,
        host=host,
        user=user,
        ssh_sources=ssh_source,
        public_addresses=public_address or [],
    )
    common.emit(edge, json_output=json_output)


@edge_app.command("update")
def edge_update(
    name: Annotated[str, typer.Argument(help="Stable edge name.")],
    public_address: Annotated[
        list[str],
        typer.Option(
            "--public-address",
            help="Replacement public CDN IP or hostname; repeat when needed.",
        ),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Replace the public addresses used for DNS and certificate checks."""
    if not public_address:
        raise typer.BadParameter("at least one --public-address is required")
    settings = common.settings()
    edge = Inventory(settings.inventory_path).set_public_addresses(name, public_address)
    common.emit(edge, json_output=json_output)


@edge_app.command("remove")
def edge_remove(
    name: str,
    yes: Annotated[bool, typer.Option("--yes")] = False,
    decommission: Annotated[
        bool,
        typer.Option(
            "--decommission/--no-decommission",
            help="Strip BlitzeCDN configuration and TLS keys from the host first.",
        ),
    ] = True,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Remove the entry even if the teardown failed. For a host that "
            "no longer exists.",
        ),
    ] = False,
) -> None:
    """Decommission an edge and remove it from desired state.

    Once the inventory entry is gone the host is unaddressable, so the teardown
    has to happen first. ``--no-decommission`` skips it for a host that was
    already wiped by other means; the files it would have removed, including
    private keys, then stay where they are.
    """
    prompt = (
        f"Remove BlitzeCDN configuration and TLS keys from {name!r}, then stop "
        "managing it?"
        if decommission
        else f"Stop managing edge {name!r} without cleaning it up?"
    )
    if not yes and not typer.confirm(prompt):
        raise typer.Abort()
    if decommission:
        DecommissionEdgeCommand(name=name, force=force).execute(
            common.control_plane(), "cli"
        )
    else:
        Inventory(common.settings().inventory_path).remove_edge(name)
    typer.echo(f"Removed {name}")
