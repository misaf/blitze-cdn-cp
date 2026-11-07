"""`edge` — the fleet roster, and the build arguments for its runtime image."""

from __future__ import annotations

from typing import Annotated

import typer

from blitzecdn.capabilities.edges import EdgeOperationsService
from blitzecdn.capabilities.edges.domain import (
    Edge,
    EdgePatch,
    HostKey,
    known_hosts_pattern,
    select_host_keys,
)
from blitzecdn.core.exceptions import ConflictError
from blitzecdn.core.features.resolution import resolve_edge_modules
from blitzecdn.entrypoints.cli import common

edge_app = typer.Typer(no_args_is_help=True, help="Manage edge servers.")

HostKeyFingerprint = Annotated[
    list[str] | None,
    typer.Option(
        "--host-key-fingerprint",
        help=(
            "The edge's SHA256 SSH host key fingerprint, from 'ssh-keygen -lf' "
            "on the edge; repeat for more. Only a matching key is trusted. Omit "
            "to be shown the scanned fingerprints and asked."
        ),
    ),
]


def _enroll_host_key(
    service: EdgeOperationsService, edge: Edge, fingerprints: list[str] | None
) -> tuple[HostKey, ...]:
    """Scan ``edge``, decide which keys to trust, and record them.

    With fingerprints, only matching keys are kept and nothing is asked. Without
    them the operator is shown what the host offered and has to confirm it; a
    run with no terminal to confirm on aborts rather than trusting on first use.
    """
    scanned = service.scan_host_keys(edge)
    if fingerprints:
        chosen = select_host_keys(scanned, fingerprints)
    else:
        typer.echo(
            f"{known_hosts_pattern(edge.host, edge.port)} offers these SSH host keys:",
            err=True,
        )
        for key in scanned:
            typer.echo(f"  {key.key_type} {key.fingerprint}", err=True)
        typer.echo(
            "Compare them with 'ssh-keygen -lf' on the edge's own console.", err=True
        )
        if not typer.confirm("Trust these host keys?", err=True):
            raise typer.Abort()
        chosen = scanned
    service.trust_host_keys(edge, chosen, "cli")
    return chosen


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
    host_key_fingerprint: HostKeyFingerprint = None,
    skip_host_key: Annotated[
        bool,
        typer.Option(
            "--skip-host-key",
            help=(
                "Register without trusting a host key, for a host that is not "
                "up yet. Run 'blitzecdn edge host-key' before deploying."
            ),
        ),
    ] = False,
    json_output: common.JsonOutput = False,
) -> None:
    """Register an edge server and trust its SSH host key.

    Nothing is converged: the edge exists from now on, so the next
    `blitzecdn deploy` includes it. There is no inventory file to write —
    Ansible reads the fleet from the control plane on every run.

    The controller connects with strict host key checking, so the edge's key is
    scanned first and trusted only once you confirm its fingerprint, or when it
    matches `--host-key-fingerprint`. On a checkout without the controller's
    SSH configuration, your own SSH client and known_hosts decide instead.

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
    if skip_host_key and host_key_fingerprint:
        raise typer.BadParameter(
            "--skip-host-key and --host-key-fingerprint contradict each other"
        )
    service = common.service(EdgeOperationsService)
    candidate = Edge(
        name=name,
        host=host,
        user=user,
        port=port,
        private_key_file=private_key_file,
        public_addresses=tuple(public_address or ()),
        ssh_sources=tuple(ssh_source),
        capabilities=None if capability is None else tuple(capability),
    )
    # Trusted before the edge is registered, so an edge that cannot be reached
    # or whose key is refused is not left in the fleet failing every deploy.
    # A taken name is refused first, before anything is scanned or trusted.
    if candidate.name in {edge.name for edge in service.list_edges()}:
        raise ConflictError(f"edge {candidate.name!r} already exists")
    if not skip_host_key and service.manages_host_keys(candidate):
        _enroll_host_key(service, candidate, host_key_fingerprint)
    edge = service.add_edge(candidate, "cli")
    next_step = (
        f"Run 'blitzecdn edge host-key {edge.name}' to trust its SSH host key, "
        "then 'blitzecdn deploy'."
        if skip_host_key and service.manages_host_keys(edge)
        else "Run 'blitzecdn deploy' to converge it."
    )
    common.emit(
        edge,
        json_output=json_output,
        note=f"\nRegistered {edge.name}. {next_step}",
    )


@edge_app.command("host-key")
def edge_host_key(
    name: Annotated[str, typer.Argument(help="Registered edge name.")],
    host_key_fingerprint: HostKeyFingerprint = None,
) -> None:
    """Trust an edge's current SSH host key, replacing the one trusted before.

    For an edge registered with `--skip-host-key` or through the API, one whose
    `--host` or `--port` changed, and an edge whose host key was rotated or
    reinstalled. The edge is scanned and its fingerprint confirmed exactly as
    on `edge add`; every key previously trusted for its address is replaced.
    """
    service = common.service(EdgeOperationsService)
    edge = service.get_edge(name)
    if not service.manages_host_keys(edge):
        raise typer.BadParameter(
            f"{edge.host}'s host key is not the controller's to manage: on a "
            "checkout your SSH client's known_hosts decides, and the loopback "
            "keys are recorded by the installer."
        )
    previous = service.trusted_host_keys(edge)
    if previous:
        typer.echo("Currently trusted:", err=True)
        for key in previous:
            typer.echo(f"  {key.key_type} {key.fingerprint}", err=True)
    trusted = _enroll_host_key(service, edge, host_key_fingerprint)
    typer.echo(
        f"Trusted {len(trusted)} host key(s) for {edge.name} "
        f"({known_hosts_pattern(edge.host, edge.port)})."
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
    service = common.service(EdgeOperationsService)
    edge = service.update_edge(name, EdgePatch.model_validate(named), "cli")
    # Host keys are trusted per address, so a new one has none yet.
    readdressed = (host is not None or port is not None) and (
        service.manages_host_keys(edge) and not service.trusted_host_keys(edge)
    )
    next_step = (
        f"Run 'blitzecdn edge host-key {edge.name}' to trust its SSH host key "
        "at the new address, then 'blitzecdn deploy'."
        if readdressed
        else "Run 'blitzecdn deploy' to apply."
    )
    common.emit(
        edge,
        json_output=json_output,
        note=f"\nUpdated {edge.name}. {next_step}",
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
