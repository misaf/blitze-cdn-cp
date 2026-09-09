"""The zone itself: add one, look at it, repoint its origin, remove it.

What this capability owns outright. Everything else in this package edits a
field that belongs to some other capability's contract, or a record.

What is not here is the list of hostnames a zone serves. Those are its proxied
records, added and removed with 'blitzecdn record add' and
'blitzecdn record proxy'.
"""

from __future__ import annotations

from typing import Annotated

import typer

from blitzecdn.capabilities.dns.cli.app import _report, _update, domain_app
from blitzecdn.capabilities.dns.domain import Domain, DomainPatch
from blitzecdn.cli import common


@domain_app.command("add")
def domain_add(
    name: Annotated[str, typer.Argument(help="Zone to serve, e.g. example.com.")],
    json_output: common.JsonOutput = False,
) -> None:
    """Register a DNS zone delegated to Blitzecdn.

    What the edge fetches from is not asked here: that is the value on each
    proxied record, and a zone is delegable before anyone has pointed a record
    at anything.
    """
    common.emit(
        common.control_plane().dns.create_domain(Domain(name=name), "cli"),
        json_output=json_output,
    )


@domain_app.command("hosts")
def domain_hosts(
    json_output: common.JsonOutput = False,
) -> None:
    """List the virtual hosts the zones and their rules resolve to.

    Nothing authors these: one per group of hostnames served alike, which is
    one per zone plus one per rule that some proxied record matched. It is the
    same document the edges are handed, and the place to look when a zone and
    a rule together are not producing what you expected.

    This is desired state, not what the fleet is running: an edit that has not
    been deployed is already here. Use 'drift' for the fleet's view.
    """
    common.emit(common.control_plane().sites.list_sites(), json_output=json_output)


@domain_app.command("show")
def domain_show(
    name: str,
    json_output: common.JsonOutput = False,
) -> None:
    """Show a zone and the policy every hostname in it is served by."""
    common.emit(common.control_plane().dns.get_domain(name), json_output=json_output)


def _cleared(no_request_host: bool, no_sni: bool) -> frozenset[str]:
    """The origin fields an explicit `--no-*` asked to put back to the default.

    `None` is a real value for both — it means "use the visitor's Host", and
    "follow the request host" — so it cannot double as "the operator did not
    mention this". These two names are how the patch is told the difference.
    """
    asked = {"origin_request_host": no_request_host, "origin_sni": no_sni}
    return frozenset(field for field, clear in asked.items() if clear)


@domain_app.command("origin")
def domain_origin(
    name: Annotated[str, typer.Argument(help="Zone, e.g. example.com.")],
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
    json_output: common.JsonOutput = False,
) -> None:
    """Set the identity the edge presents on this zone's origin leg.

    Where the connection actually goes is the value on each proxied record —
    'blitzecdn record add <zone> <name> --value <origin>'. What is set here is
    *as whom* the edge reaches it: `--request-host` is the `Host` header the
    origin sees, which a shared host routes on, so a backend serving many zones
    from one address needs it to tell them apart. `--sni` is the name offered
    in the TLS handshake, which is what a backend presents a certificate for;
    it follows `--request-host` unless a certificate is issued for something
    else.

    Both are clearable — `--no-request-host` and `--no-sni` put the zone back
    on the default — because either is a value whose absence means something,
    and a setting an operator can turn on but never off is one they have to
    edit the database to undo.

    Options you do not name are left as they are.
    """
    for value, clear, flag in (
        (request_host, no_request_host, "request-host"),
        (sni, no_sni, "sni"),
    ):
        if value is not None and clear:
            raise typer.BadParameter(f"--{flag} and --no-{flag} contradict each other")
    supplied: dict[str, object] = {
        "origin_request_host": None if no_request_host else request_host,
        "origin_sni": None if no_sni else sni,
    }
    named = {
        field: value
        for field, value in supplied.items()
        if value is not None or field in _cleared(no_request_host, no_sni)
    }
    if not named:
        raise typer.BadParameter("give at least one of --request-host or --sni")
    # Built from the dict rather than by keyword: a patch applies the fields
    # that were *set*, and `origin_sni=None` passed for an option nobody named
    # would clear the override instead of leaving it alone. Going through the
    # dict is what keeps "not mentioned" and "explicitly cleared" distinct.
    zone = _update(name, DomainPatch.model_validate(named))
    identity = zone.origin_request_host or "the visitor's Host header"
    _report(
        zone,
        f"{zone.name} reaches its origin as {identity}.",
        json_output=json_output,
    )


@domain_app.command("enable")
def domain_enable(
    name: Annotated[str, typer.Argument(help="Zone, e.g. example.com.")],
    on: Annotated[
        bool, typer.Option("--on/--off", help="Serve this zone, or withdraw it.")
    ],
    json_output: common.JsonOutput = False,
) -> None:
    """Serve or withdraw a zone without touching its records.

    A disabled zone keeps its policy, its rules and its records; it simply
    stops being converged. Unproxying the records instead would change what DNS
    answers with, which is a different decision.
    """
    zone = _update(name, DomainPatch(enabled=on))
    _report(
        zone,
        f"{zone.name} is now {'enabled' if on else 'disabled'}.",
        json_output=json_output,
    )


@domain_app.command("list")
def domain_list(json_output: common.JsonOutput = False) -> None:
    """List the DNS zones delegated to BlitzeCDN."""
    common.emit(common.control_plane().dns.list_domains(), json_output=json_output)


@domain_app.command("remove")
def domain_remove(
    name: str,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Remove a zone and every record in it.

    The sites those records routed to are left alone. They stop being reachable
    at the next deploy, because nothing answers for their hostnames any more,
    and their configuration survives for a record that points back at them.
    """
    if not yes and not typer.confirm(f"Delete {name!r} and all of its records?"):
        raise typer.Abort()
    common.control_plane().dns.delete_domain(name, "cli")
    typer.echo(f"Deleted {name}")
