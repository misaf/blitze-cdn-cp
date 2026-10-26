"""`edge` — the fleet roster, and the build arguments for its runtime image."""

from __future__ import annotations

from typing import Annotated

import typer

from blitzecdn.capabilities.edges import EdgeOperationsService
from blitzecdn.capabilities.edges.domain import Edge, EdgePatch
from blitzecdn.core.features.resolution import resolve_edge_modules
from blitzecdn.entrypoints.cli import common

edge_app = typer.Typer(no_args_is_help=True, help="Manage edge servers.")


@edge_app.command("list")
def edge_list(json_output: common.JsonOutput = False) -> None:
    """List edge servers.

    This is exactly what Ansible is given: the same rows the `blitzecdn`
    inventory plugin publishes. To see them as Ansible sees them, including the
    group variables that apply on top:

        ansible-inventory -i ansible/inventory/blitzecdn.yml --list
    """
    common.emit(
        common.service(EdgeOperationsService).list_edges(), json_output=json_output
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
    capability: Annotated[
        list[str] | None,
        typer.Option(
            "--capability",
            help=(
                "A capability this edge's runtime provides; repeat for more. "
                "Omit to assume whatever the controller has installed."
            ),
        ),
    ] = None,
    json_output: common.JsonOutput = False,
) -> None:
    """Register an edge server.

    Nothing is converged and nothing reaches the host: the edge exists from now
    on, so the next `blitzecdn deploy` includes it. There is no inventory file
    to write — Ansible reads the fleet from the control plane on every run.

    `--capability` is what this edge's runtime image can actually do. Naming
    them makes compilation refuse a site whose settings this edge could not
    serve — Brotli on an nginx that never loaded the module is a syntax error
    at converge time — before a run starts rather than partway through one.
    Omitting them means the edge provides whatever the controller has
    installed.
    """
    if not ssh_source:
        raise typer.BadParameter(
            "at least one --ssh-source management CIDR is required"
        )
    edge = common.service(EdgeOperationsService).add_edge(
        Edge(
            name=name,
            host=host,
            user=user,
            port=port,
            private_key_file=private_key_file,
            public_addresses=tuple(public_address or ()),
            ssh_sources=tuple(ssh_source),
            capabilities=None if capability is None else tuple(capability),
        ),
        "cli",
    )
    common.emit(
        edge,
        json_output=json_output,
        note=f"\nRegistered {edge.name}. Run 'blitzecdn deploy' to converge it.",
    )


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
    capability: Annotated[
        list[str] | None,
        typer.Option(
            "--capability",
            help="Replacement capability token for this edge's runtime; repeat.",
        ),
    ] = None,
    json_output: common.JsonOutput = False,
) -> None:
    """Change an edge's connection details, addresses, or declared capabilities.

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
        "capabilities": None if capability is None else tuple(capability),
    }
    named = {field: value for field, value in supplied.items() if value is not None}
    if not named:
        raise typer.BadParameter("give at least one field to change")
    edge = common.service(EdgeOperationsService).update_edge(
        name, EdgePatch.model_validate(named), "cli"
    )
    common.emit(
        edge,
        json_output=json_output,
        note=f"\nUpdated {edge.name}. Run 'blitzecdn deploy' to apply.",
    )


@edge_app.command("remove")
def edge_remove(
    name: str,
    yes: common.Yes = False,
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
    if decommission:
        common.service(EdgeOperationsService).decommission_edge(
            name, "cli", force=force
        )
    else:
        common.service(EdgeOperationsService).remove_edge(name, "cli")
    typer.echo(f"Removed {name}")


image_app = typer.Typer(
    no_args_is_help=True,
    help="The edge runtime image the fleet serves from.",
)
edge_app.add_typer(image_app, name="image")


@image_app.command("spec")
def edge_image_spec(
    json_output: common.JsonOutput = False,
) -> None:
    """Print the build arguments the edge runtime image is built from.

    The image needs to know which Nginx dynamic modules to carry, as declared by
    the capabilities, in the form `docker build --build-arg` takes:

        docker build $(blitzecdn edge image spec | sed 's/^/--build-arg /') \\
            "$(python -c 'import blitzecdn.assets.docker as d; print(d.EDGE_CONTEXT)')"

    This is the superset every image should carry; each edge mounts its own
    narrower `load_module` list. Needs no database, so it runs in an image build.
    """
    modules = resolve_edge_modules(common.installed_features().ansible_contributions())
    if json_output:
        common.emit(
            [
                {
                    "feature": module.feature,
                    "name": module.name,
                    "objects": list(module.objects),
                    "build": module.build,
                    "probe": module.probe,
                }
                for module in modules
            ],
            json_output=True,
        )
        return
    # One `NAME=value` per build argument, as `docker build --build-arg` and the
    # GitHub build action take; values space separated for the Dockerfile's loops.
    typer.echo(
        "ENABLED_MODULES=" + " ".join(module.name for module in modules if module.build)
    )
    typer.echo(
        "LOADED_MODULES="
        + " ".join(
            shared_object for module in modules for shared_object in module.objects
        )
    )
    typer.echo(
        "MODULE_PROBE_DIRECTIVES="
        + " ".join(module.probe for module in modules if module.probe)
    )
