"""`edge` and `origin` — the fleet itself, and what it proxies to."""

from __future__ import annotations

from typing import Annotated

import typer

from blitzecdn.cli import common
from blitzecdn.cli.common import ExitCode
from blitzecdn.features.edges.domain import Edge, EdgePatch

edge_app = typer.Typer(no_args_is_help=True, help="Manage edge servers.")
origin_app = typer.Typer(
    no_args_is_help=True, help="Check the origins the edges proxy to."
)


@origin_app.command("check")
def origin_check(
    limit: Annotated[str | None, common.LIMIT_OPTION] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Ask the edges to connect to the origins they proxy to.

    Catches the misconfigurations `validate` cannot see — an origin that does
    not resolve, is not listening, or presents a certificate that does not
    match the SNI the edge sends. Exits 3 if any edge could not reach one.

    The edges answer, not the controller: they are the machines that carry the
    traffic, and an origin that allow-lists them refuses the controller while
    working perfectly. Each failing site is reported with the edges that could
    not reach it — an origin no edge can reach is down, while one only some can
    reach is a routing or allow-list problem.
    """
    report = common.control_plane().edges.check_origins("cli", host_limit=limit)
    common.emit(report, json_output=json_output)
    if json_output:
        if report.failing_sites:
            raise typer.Exit(ExitCode.CONFIGURATION)
        return
    if not report.reporting:
        typer.echo("No edge reported an origin check.")
    elif not report.failing_sites:
        checked = sum(len(edge.checks) for edge in report.reporting)
        typer.echo(
            f"\n{checked} origin check(s) across "
            f"{len(report.reporting)} edge(s) answered as expected."
        )
    for edge in report.silent:
        typer.echo(f"\n{edge.host}: {edge.error}", err=True)
    for site, hosts in report.failing_sites.items():
        typer.echo(f"\n{site}: unreachable from {', '.join(hosts)}", err=True)
    if report.failing_sites:
        raise typer.Exit(ExitCode.CONFIGURATION)


@edge_app.command("list")
def edge_list(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List edge servers.

    This is exactly what Ansible is given: the same rows the `blitzecdn`
    inventory plugin publishes. To see them as Ansible sees them, including the
    group variables that apply on top:

        ansible-inventory -i ansible/inventory/blitzecdn.yml --list
    """
    common.emit(common.control_plane().edges.list_edges(), json_output=json_output)


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
    port: Annotated[
        int,
        typer.Option(
            "--port",
            min=1,
            max=65535,
            help="SSH port. Must match blitzecdn_firewall_ssh_port on the edge.",
        ),
    ] = 22,
    private_key_file: Annotated[
        str | None,
        typer.Option(
            "--private-key-file",
            help="SSH private key for this edge. Omit to let SSH resolve one.",
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Register an edge server.

    Nothing is converged and nothing reaches the host: the edge exists from now
    on, so the next `blitzecdn deploy` includes it. There is no inventory file
    to write — Ansible reads the fleet from the control plane on every run.
    """
    if not ssh_source:
        raise typer.BadParameter(
            "at least one --ssh-source management CIDR is required"
        )
    edge = common.control_plane().edges.add_edge(
        Edge(
            name=name,
            host=host,
            user=user,
            port=port,
            private_key_file=private_key_file,
            public_addresses=tuple(public_address or ()),
            ssh_sources=tuple(ssh_source),
        ),
        "cli",
    )
    common.emit(edge, json_output=json_output)
    if not json_output:
        typer.echo(f"\nRegistered {edge.name}. Run 'blitzecdn deploy' to converge it.")


@edge_app.command("update")
def edge_update(
    name: Annotated[str, typer.Argument(help="Stable edge name.")],
    host: Annotated[
        str | None, typer.Option("--host", help="Replacement SSH hostname or address.")
    ] = None,
    user: Annotated[
        str | None, typer.Option("--user", help="Replacement non-root SSH user.")
    ] = None,
    port: Annotated[
        int | None,
        typer.Option("--port", min=1, max=65535, help="Replacement SSH port."),
    ] = None,
    private_key_file: Annotated[
        str | None,
        typer.Option("--private-key-file", help="Replacement SSH private key path."),
    ] = None,
    public_address: Annotated[
        list[str] | None,
        typer.Option(
            "--public-address",
            help="Replacement public CDN IP or hostname; repeat when needed.",
        ),
    ] = None,
    ssh_source: Annotated[
        list[str] | None,
        typer.Option(
            "--ssh-source",
            help="Replacement management CIDR; repeat when needed.",
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Change an edge's connection details or public addresses.

    Each option replaces its own value; anything you do not name is kept. The
    two list options replace their whole list rather than appending, so
    removing the last entry is expressible.

    The name cannot be changed: certificates, audit history and `--limit` all
    refer to it. Remove the edge and add it again under the new name.
    """
    supplied = {
        "host": host,
        "user": user,
        "port": port,
        "private_key_file": private_key_file,
        "public_addresses": None if public_address is None else tuple(public_address),
        "ssh_sources": None if ssh_source is None else tuple(ssh_source),
    }
    named = {field: value for field, value in supplied.items() if value is not None}
    if not named:
        raise typer.BadParameter("give at least one field to change")
    edge = common.control_plane().edges.update_edge(
        name, EdgePatch.model_validate(named), "cli"
    )
    common.emit(edge, json_output=json_output)
    if not json_output:
        typer.echo(f"\nUpdated {edge.name}. Run 'blitzecdn deploy' to apply.")


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

    Once the edge is gone from the control plane the host is unaddressable —
    the inventory is derived from those same rows — so the teardown has to
    happen first. ``--no-decommission`` skips it for a host that was already
    wiped by other means; the files it would have removed, including private
    keys, then stay where they are.
    """
    prompt = (
        f"Remove BlitzeCDN configuration and TLS keys from {name!r}, then stop "
        "managing it?"
        if decommission
        else f"Stop managing edge {name!r} without cleaning it up?"
    )
    if not yes and not typer.confirm(prompt):
        raise typer.Abort()
    control = common.control_plane()
    if decommission:
        control.edges.decommission_edge(name, "cli", force=force)
    else:
        control.edges.remove_edge(name, "cli")
    typer.echo(f"Removed {name}")
