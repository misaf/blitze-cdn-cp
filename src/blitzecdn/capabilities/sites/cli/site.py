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


def _cleared(no_request_host: bool, no_sni: bool) -> frozenset[str]:
    """The origin fields an explicit `--no-*` asked to put back to the default.

    `None` is a real value for both — it means "use the visitor's Host", and
    "follow the request host" — so it cannot double as "the operator did not
    mention this". These two names are how the patch is told the difference.
    """
    asked = {"origin_request_host": no_request_host, "origin_sni": no_sni}
    return frozenset(field for field, clear in asked.items() if clear)


@site_app.command("origin")
def site_origin(
    name: Annotated[str, typer.Argument()],
    origin: Annotated[
        str | None,
        typer.Option("--origin", help="Host or address the edge fetches from."),
    ] = None,
    request_host: Annotated[
        str | None,
        typer.Option(
            "--request-host",
            help="Host header sent on the origin leg. Defaults to the visitor's.",
        ),
    ] = None,
    no_request_host: Annotated[
        bool,
        typer.Option("--no-request-host", help="Forward the visitor's Host again."),
    ] = False,
    sni: Annotated[
        str | None,
        typer.Option(
            "--sni",
            help="TLS server name sent to the origin. Defaults to --request-host.",
        ),
    ] = None,
    no_sni: Annotated[
        bool,
        typer.Option("--no-sni", help="Offer the request host as the TLS name again."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Change where the edge fetches this site's content from, and as whom.

    Three settings rather than one because the address and the identity are
    separate questions. `--origin` is where the connection goes. `--request-host`
    is the `Host` header the origin sees, which a shared host routes on, so an
    origin serving many sites from one address needs it to tell them apart.
    `--sni` is the name offered in the TLS handshake, which is what an origin
    presents a certificate for; it follows `--request-host` unless a certificate
    is issued for something else.

    Both overrides are clearable — `--no-request-host` and `--no-sni` put the
    site back on the default — because either is a value whose absence means
    something, and a setting an operator can turn on but never off is one they
    have to edit the database to undo.

    Options you do not name are left as they are.
    """
    for value, clear, flag in (
        (request_host, no_request_host, "request-host"),
        (sni, no_sni, "sni"),
    ):
        if value is not None and clear:
            raise typer.BadParameter(f"--{flag} and --no-{flag} contradict each other")
    supplied: dict[str, object] = {
        "origin_host": origin,
        "origin_request_host": None if no_request_host else request_host,
        "origin_sni": None if no_sni else sni,
    }
    named = {
        field: value
        for field, value in supplied.items()
        if value is not None or field in _cleared(no_request_host, no_sni)
    }
    if not named:
        raise typer.BadParameter(
            "give at least one of --origin, --request-host or --sni"
        )
    # Built from the dict rather than by keyword: a patch applies the fields
    # that were *set*, and `origin_sni=None` passed for an option nobody named
    # would clear the override instead of leaving it alone. Going through the
    # dict is what keeps "not mentioned" and "explicitly cleared" distinct.
    site = _update(name, SitePatch.model_validate(named))
    common.emit(site, json_output=json_output)
    if not json_output:
        identity = site.origin_request_host or "the visitor's Host header"
        typer.echo(
            _applied(
                site,
                f"{site.name} now fetches from {site.origin_host} as {identity}.",
            )
        )


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
