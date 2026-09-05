"""The site itself: create one, look at it, repoint it, withdraw it, delete it.

What this capability owns outright. Everything else in this package edits a
field that belongs to some other capability\'s contract.

What is still not here is `server_names`: the hostnames a site answers on are
the records routed to it, so they are added and removed with
\'blitzecdn record route\' and \'blitzecdn record unroute\'.
"""

from __future__ import annotations

from typing import Annotated

import typer

from blitzecdn.capabilities.sites.cli.app import _applied, _update, site_app
from blitzecdn.capabilities.sites.domain import CdnSite, SitePatch
from blitzecdn.cli import common


@site_app.command("create")
def site_create(
    name: Annotated[
        str, typer.Argument(help="Internal site name, e.g. www-example-com.")
    ],
    origin: Annotated[
        str, typer.Option("--origin", help="Host or address the edge fetches from.")
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Create a virtual host.

    It serves nothing until a DNS record routes a hostname to it. Everything
    else — TLS, caching, compression, firewall — has a default and its own
    command; the site is usable as soon as it exists.
    """
    site = CdnSite(name=name, origin_host=origin)
    created = common.control_plane().site_editor.create_site(site, "cli")
    common.emit(created, json_output=json_output)
    if not json_output:
        typer.echo(
            f"Created site {created.name!r} fetching from {created.origin_host}. "
            f"Route a hostname to it: blitzecdn record route <zone> <name> "
            f"--site {created.name}"
        )


@site_app.command("list")
def site_list(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List the virtual hosts.

    This is desired state, not what the fleet is running: a site edited but not
    deployed is already listed here. Use 'drift' for the fleet's view.
    """
    common.emit(common.control_plane().sites.list_sites(), json_output=json_output)


@site_app.command("show")
def site_show(
    name: Annotated[str, typer.Argument(help="Site name, e.g. www-example-com.")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show the fully resolved policy for one site.

    Where the defaults a create never mentioned become visible — it is the same
    document the edges are handed.
    """
    common.emit(common.control_plane().sites.get_site(name), json_output=json_output)


@site_app.command("origin")
def site_origin(
    name: Annotated[str, typer.Argument()],
    origin: Annotated[
        str, typer.Option("--origin", help="Host or address the edge fetches from.")
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Change where the edge fetches this site's content from."""
    site = _update(name, SitePatch(origin_host=origin))
    common.emit(site, json_output=json_output)
    if not json_output:
        typer.echo(_applied(site, f"{site.name} now fetches from {site.origin_host}."))


@site_app.command("enable")
def site_enable(
    name: Annotated[str, typer.Argument()],
    on: Annotated[
        bool, typer.Option("--on/--off", help="Serve this site, or withdraw it.")
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Serve or withdraw a site without touching its records.

    A disabled site keeps its name, its hostnames and every setting; it simply
    stops being converged. Deleting the records instead would take the
    hostnames back out of DNS, which is a different decision.
    """
    site = _update(name, SitePatch(enabled=on))
    common.emit(site, json_output=json_output)
    if not json_output:
        typer.echo(
            _applied(site, f"{site.name} is now {'enabled' if on else 'disabled'}.")
        )


@site_app.command("remove")
def site_remove(
    name: Annotated[str, typer.Argument()],
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Delete a site that no hostname routes to.

    Refused while records still point here, and the refusal names them: the
    order is yours to choose. To stop serving a site but keep it, use
    'site enable --off'.
    """
    if not yes and not typer.confirm(f"Delete site {name!r}?"):
        raise typer.Abort()
    common.control_plane().site_editor.delete_site(name, "cli")
    typer.echo(f"Deleted {name}")
