"""The `security` contract\'s switches: the request filter and Under Attack Mode.

Edits fields declared by :mod:`blitzecdn.capabilities.security.policy`. Both
need an implementation on the fleet — `blitzecdn-security`, and
`blitzecdn-geoip` for the country rules — and a deploy refuses rather than
silently serve traffic a rule here was meant to block.
"""

from __future__ import annotations

from typing import Annotated

import typer

from blitzecdn.capabilities.dns.cli.app import _report, _update, domain_app
from blitzecdn.capabilities.dns.domain import DomainPatch
from blitzecdn.capabilities.security.policy import SiteFirewall
from blitzecdn.cli import common


@domain_app.command("under-attack")
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
    zone = _update(name, DomainPatch(under_attack_mode=on))
    _report(
        zone,
        f"Under Attack Mode is now {'enabled' if on else 'disabled'} for {zone.name}.",
        json_output=json_output,
    )


@domain_app.command("firewall")
def domain_firewall(
    name: Annotated[str, typer.Argument(help="Zone, e.g. example.com.")],
    allow_source: Annotated[
        list[str] | None,
        typer.Option(
            "--allow-source",
            help="CIDR exempted from --deny-source. Repeatable. Replaces the list.",
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
    deny_method: Annotated[
        list[str] | None,
        typer.Option("--deny-method", help="HTTP method answered with 405."),
    ] = None,
    deny_path: Annotated[
        list[str] | None,
        typer.Option("--deny-path", help="URI prefix answered with 403."),
    ] = None,
    clear: Annotated[
        bool, typer.Option("--clear", help="Remove every rule and serve everyone.")
    ] = False,
    json_output: common.JsonOutput = False,
) -> None:
    """Filter requests to one zone at the edge.

    The posture stays open: rules subtract from a zone that otherwise serves
    everyone, and --allow-source is an exemption from --deny-source rather than
    a whitelist. To close a zone, deny everything and list the exceptions:

        blitzecdn zone firewall www-example-com \\
            --deny-source 0.0.0.0/0 --deny-source ::/0 \\
            --allow-source 203.0.113.0/24

    Each option replaces its own list; the lists you do not name are kept. That
    differs from the equivalent PATCH on the HTTP API, which replaces the whole
    block. Country rules need the 'geoip' capability on this controller — the
    blitzecdn-geoip distribution — and blitzecdn_geoip_enabled with a
    country database on the edge. A deploy will refuse rather than silently
    serve the traffic they were meant to block.
    """
    control = common.control_plane()
    supplied = {
        "allow_sources": allow_source,
        "deny_sources": deny_source,
        "allowed_countries": allow_country,
        "denied_countries": deny_country,
        "denied_methods": deny_method,
        "denied_paths": deny_path,
    }
    named = {field: value for field, value in supplied.items() if value is not None}
    if clear and named:
        raise typer.BadParameter("--clear cannot be combined with a rule option")
    if not clear and not named:
        raise typer.BadParameter(
            "give at least one rule option, or --clear to remove every rule"
        )
    if clear:
        firewall = SiteFirewall()
    else:
        # Merged as a plain mapping and revalidated, rather than model_copy'd:
        # model_copy would install the raw lists without running a validator,
        # and these end up interpolated into an nginx directive.
        current = control.dns.get_domain(name).firewall
        firewall = SiteFirewall.model_validate(current.model_dump() | named)
    zone = _update(name, DomainPatch(firewall=firewall))
    rules = sum(len(getattr(zone.firewall, f)) for f in SiteFirewall.model_fields)
    _report(
        zone,
        f"{zone.name} now carries {rules} firewall rule(s)."
        if rules
        else f"{zone.name} no longer filters any requests.",
        json_output=json_output,
    )
