"""The `security` contract's switches: the request filter and Under Attack Mode."""

from __future__ import annotations

from typing import Annotated

import typer

from blitzecdn.capabilities.security.contract import RATE_LIMIT_RATES, SecurityPolicy
from blitzecdn.capabilities.security.domain import EditFirewall
from blitzecdn.capabilities.sites import DomainPatch, SiteService
from blitzecdn.delivery.cli import common
from blitzecdn.delivery.cli.domain_editing import report, update

#: Joined to `blitzecdn domain`, which dns founds: these edit the zone's
#: security settings, so they ship with the capability that owns them.
domain_commands = typer.Typer()


@domain_commands.command("under-attack")
def domain_under_attack(
    name: Annotated[str, typer.Argument(help="Zone, e.g. example.com.")],
    on: Annotated[
        bool,
        typer.Option(
            "--on/--off",
            help="Challenge unverified browser traffic at the edge, or disable it.",
        ),
    ],
    json_output: common.JsonOutput = False,
) -> None:
    """Enable or disable emergency edge challenge/mitigation mode.

    Enabling this policy also requires the fleet's Nginx under-attack
    capability and a signing secret. A deploy fails rather than ignoring a
    zone whose edge cannot enforce it.
    """
    zone = update(name, DomainPatch.model_validate({"under_attack_mode": on}))
    report(
        zone,
        f"Under Attack Mode is now {'enabled' if on else 'disabled'} for {zone.name}.",
        json_output=json_output,
    )


@domain_commands.command("firewall")
def domain_firewall(
    name: Annotated[str, typer.Argument(help="Zone, e.g. example.com.")],
    allow_source: Annotated[
        list[str] | None,
        typer.Option(
            "--allow-source",
            help=(
                "CIDR exempted from --deny-source and from the country and method "
                "rules; path rules still apply. Repeatable. Replaces the list."
            ),
        ),
    ] = None,
    deny_source: Annotated[
        list[str] | None,
        typer.Option(
            "--deny-source", help="CIDR answered with 403. Repeatable. Replaces."
        ),
    ] = None,
    allow_country: Annotated[
        list[str] | None,
        typer.Option(
            "--allow-country",
            help="ISO 3166-1 alpha-2; every other country gets a 403. Needs GeoIP.",
        ),
    ] = None,
    deny_country: Annotated[
        list[str] | None,
        typer.Option(
            "--deny-country",
            help="ISO 3166-1 alpha-2 answered with 403. Needs GeoIP on the edge.",
        ),
    ] = None,
    allow_method: Annotated[
        list[str] | None,
        typer.Option(
            "--allow-method",
            help=(
                "HTTP method served; every other method gets a 405. GET brings "
                "HEAD. Allowing only GET also refuses CORS preflight (OPTIONS) "
                "and form posts. Repeatable. Replaces the list."
            ),
        ),
    ] = None,
    deny_method: Annotated[
        list[str] | None,
        typer.Option("--deny-method", help="HTTP method answered with 405."),
    ] = None,
    allow_path: Annotated[
        list[str] | None,
        typer.Option(
            "--allow-path",
            help=(
                "Path served; every other path gets a 403. A trailing '/' admits "
                "everything under it. Repeatable. Replaces the list."
            ),
        ),
    ] = None,
    deny_path: Annotated[
        list[str] | None,
        typer.Option("--deny-path", help="URI prefix answered with 403."),
    ] = None,
    case_insensitive_paths: Annotated[
        bool | None,
        typer.Option(
            "--case-insensitive-paths/--case-sensitive-paths",
            help=(
                "Match --allow-path and --deny-path regardless of letter case, "
                "for origins that treat /Admin as /admin. Off by default."
            ),
        ),
    ] = None,
    clear: Annotated[
        bool, typer.Option("--clear", help="Remove every rule and serve everyone.")
    ] = False,
    json_output: common.JsonOutput = False,
) -> None:
    """Filter requests to one zone at the edge.

    The posture stays open: rules subtract from a zone that otherwise serves
    everyone, and --allow-source is an exemption rather than a whitelist. It
    exempts from --deny-source and from the country and method rules, so an
    office network GeoIP places in a denied country is still served; the path
    rules apply to it like anyone else. To close a zone, deny everything and
    list the exceptions:

        blitzecdn domain firewall example.com \\
            --deny-source 0.0.0.0/0 --deny-source ::/0 \\
            --allow-source 203.0.113.0/24

    Methods and paths have allowlists of their own, which combine: this serves
    only GET and HEAD under /api/, with /api/admin still refused:

        blitzecdn domain firewall example.com \\
            --allow-method GET --allow-path /api/ --deny-path /api/admin

    Paths match case-sensitively, as written. When the origin treats /Admin
    as /admin (IIS, ASP.NET, many CMSs), add --case-insensitive-paths, or a
    different spelling reaches the path a rule was meant to refuse.

    Each option replaces its own list; the lists you do not name are kept. That
    differs from the equivalent PATCH on the HTTP API, which replaces the whole
    block. Country rules need the 'geoip' capability, and
    blitzecdn_geoip_enabled with a country database on the controller. A deploy will
    refuse rather than silently serve the traffic they were meant to block.
    """
    supplied = {
        "allow_sources": allow_source,
        "deny_sources": deny_source,
        "allowed_countries": allow_country,
        "denied_countries": deny_country,
        "allowed_methods": allow_method,
        "denied_methods": deny_method,
        "allowed_paths": allow_path,
        "denied_paths": deny_path,
        "case_insensitive_paths": case_insensitive_paths,
    }
    named = {field: value for field, value in supplied.items() if value is not None}
    if clear and named:
        raise typer.BadParameter("--clear cannot be combined with a rule option")
    if not clear and not named:
        raise typer.BadParameter(
            "give at least one rule option, or --clear to remove every rule"
        )
    zone = common.service(SiteService).update_domain(
        name, EditFirewall.model_validate(named | {"clear": clear}), "cli"
    )
    rules = zone.policy[SecurityPolicy].firewall.rule_count
    report(
        zone,
        f"{zone.name} now carries {rules} firewall rule(s)."
        if rules
        else f"{zone.name} no longer filters any requests.",
        json_output=json_output,
    )


@domain_commands.command("rate-limit")
def domain_rate_limit(
    name: Annotated[str, typer.Argument(help="Zone, e.g. example.com.")],
    rate: Annotated[
        str | None,
        typer.Option(
            "--rate",
            help="Requests per client address: " + ", ".join(RATE_LIMIT_RATES) + ".",
        ),
    ] = None,
    burst: Annotated[
        int,
        typer.Option(
            "--burst",
            min=0,
            max=10000,
            help="Requests allowed above the rate before refusing; served at once.",
        ),
    ] = 0,
    status: Annotated[
        int,
        typer.Option("--status", help="Status refused requests get: 429 or 503."),
    ] = 429,
    path: Annotated[
        list[str] | None,
        typer.Option(
            "--path",
            help=(
                "Path the limit counts; a trailing '/' covers everything under "
                "it. Repeatable. Without it every request counts."
            ),
        ),
    ] = None,
    off: Annotated[
        bool, typer.Option("--off", help="Remove the limit and serve everyone.")
    ] = False,
    json_output: common.JsonOutput = False,
) -> None:
    """Cap how fast each client address may request one zone at the edge.

    Requests over the rate are refused with --status (429 by default) and
    show in the access log and traffic accounting like any other response.
    Addresses in the zone's --allow-source firewall list are never limited.
    The whole limit is replaced on each call:

        blitzecdn domain rate-limit example.com --rate 10r/s --burst 20

        blitzecdn domain rate-limit example.com --rate 10r/m --path /login
    """
    if off and rate is not None:
        raise typer.BadParameter("--off cannot be combined with --rate")
    if not off and rate is None:
        raise typer.BadParameter("give --rate, or --off to remove the limit")
    limit = (
        {}
        if off
        else {"rate": rate, "burst": burst, "status": status, "paths": path or []}
    )
    zone = update(name, DomainPatch.model_validate({"rate_limit": limit}))
    current = zone.policy[SecurityPolicy].rate_limit
    report(
        zone,
        f"{zone.name} now limits each client to {current.rate}."
        if current.rate
        else f"{zone.name} no longer limits request rates.",
        json_output=json_output,
    )
