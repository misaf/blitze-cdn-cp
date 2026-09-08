"""`domain`, `record` and `dns` — the zone editor's command groups.

A zone now carries the policy every hostname in it is served by, and `domain
show` and `domain origin` are what edit the part of it that is new. The
per-setting commands — `cache`, `ssl`, `firewall` and the rest — are still
`site` commands over in the capability that owns them today. They are not
duplicated here on purpose: they move to this group when `sites` is removed,
and writing a second copy of nine hundred lines meanwhile would leave two sets
of flags to keep in step for exactly as long as it took to delete one.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer

from blitzecdn.capabilities.dns.domain import (
    DnsRecord,
    Domain,
    DomainPatch,
    RecordType,
    Rule,
    RulePatch,
)
from blitzecdn.cli import common

domain_app = typer.Typer(no_args_is_help=True, help="Manage DNS zones.")
record_app = typer.Typer(
    no_args_is_help=True,
    help="Manage DNS records and the site each hostname routes to.",
)
dns_app = typer.Typer(no_args_is_help=True, help="Export DNS state.")


# -- Domains -------------------------------------------------------------


@domain_app.command("add")
def domain_add(
    name: Annotated[str, typer.Argument(help="Zone to serve, e.g. example.com.")],
    origin: Annotated[
        str | None,
        typer.Option(
            "--origin",
            help="Default origin for proxied hostnames in this zone.",
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Register a DNS zone delegated to BlitzeCDN.

    The origin is optional here and can be set later with 'domain origin'. A
    zone is delegable before anyone has decided what it proxies to, and asking
    for one up front only invites a placeholder in the field that matters most.
    """
    common.emit(
        common.control_plane().dns.create_domain(
            Domain(name=name, origin_host=origin), "cli"
        ),
        json_output=json_output,
    )


@domain_app.command("show")
def domain_show(
    name: str,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show a zone and the policy every hostname in it is served by."""
    common.emit(common.control_plane().dns.get_domain(name), json_output=json_output)


@domain_app.command("origin")
def domain_origin(
    name: str,
    origin: Annotated[
        str,
        typer.Argument(help="Hostname the edge fetches from, e.g. origin.example.com."),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Set the default origin for proxied hostnames in a zone."""
    common.emit(
        common.control_plane().dns.update_domain(
            name, DomainPatch(origin_host=origin), "cli"
        ),
        json_output=json_output,
    )


@domain_app.command("list")
def domain_list(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
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


# -- Records -------------------------------------------------------------


@record_app.command("add")
def record_add(
    domain: Annotated[str, typer.Argument(help="Zone the record belongs to.")],
    name: Annotated[str, typer.Argument(help="Subdomain label, '@', or '*'.")],
    value: Annotated[
        str | None,
        typer.Option("--value", help="IP address to answer with. Bypasses the CDN."),
    ] = None,
    site: Annotated[
        str | None,
        typer.Option("--site", help="Name of the site that serves this hostname."),
    ] = None,
    type_: Annotated[RecordType, typer.Option("--type")] = RecordType.A,
    ttl: Annotated[int, typer.Option("--ttl")] = 300,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Add a DNS record, answering with an address or routing to a site.

    Exactly one of --value and --site. With --site the edge serves the hostname
    and the site holds the origin and the policy; create it first with
    'blitzecdn site create'. With --value the record bypasses the CDN and
    resolves straight to that address.

    A dual-stack hostname is two records — one A, one AAAA — naming the same
    site. That is one virtual host, and both records are required to name the
    same site.
    """
    if (value is None) == (site is None):
        raise typer.BadParameter("give exactly one of --value and --site")
    record = DnsRecord(
        domain=domain, name=name, type=type_, value=value, ttl=ttl, site=site
    )
    common.emit(
        common.control_plane().dns.create_record(record, "cli"),
        json_output=json_output,
    )


@record_app.command("list")
def record_list(
    domain: Annotated[str | None, typer.Argument()] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List records and what each one answers with.

    Shows every zone unless you name one. A record carries either an address or
    the name of the site that serves its hostname; the policy behind that site
    is 'blitzecdn site show'.
    """
    common.emit(
        common.control_plane().dns.list_records(domain), json_output=json_output
    )


@record_app.command("route")
def record_route(
    domain: Annotated[str, typer.Argument()],
    name: Annotated[str, typer.Argument()],
    site: Annotated[str, typer.Option("--site", help="Site that serves the hostname.")],
    type_: Annotated[RecordType, typer.Option("--type")] = RecordType.A,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Put a hostname on the edge, served by a site.

    Takes effect on the edge at the next deploy. It only reaches clients once
    DNS answers with an edge address, which the DNS system owns.
    """
    record = common.control_plane().dns.route_to_site(domain, name, type_, site, "cli")
    common.emit(record, json_output=json_output)
    if not json_output:
        typer.echo(
            f"{record.fqdn} is now served by site {site!r}. Run "
            "'blitzecdn deploy' to apply, and make sure DNS points at an edge."
        )


@record_app.command("unroute")
def record_unroute(
    domain: Annotated[str, typer.Argument()],
    name: Annotated[str, typer.Argument()],
    value: Annotated[
        str, typer.Option("--value", help="Address DNS should answer with instead.")
    ],
    type_: Annotated[RecordType, typer.Option("--type")] = RecordType.A,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Take a hostname off the edge, answering with an address instead.

    The address is required rather than inferred from the site's origin. That
    is deliberate: answering with the origin address is what publishes an
    origin the CDN existed to keep private, and it should be a thing you asked
    for rather than a default.

    The site is left as it is. If nothing else routes to it, it simply stops
    being served.
    """
    record = common.control_plane().dns.stop_routing(domain, name, type_, value, "cli")
    common.emit(record, json_output=json_output)
    if not json_output:
        typer.echo(
            f"{record.fqdn} now bypasses the CDN and answers with {value}. "
            "Run 'blitzecdn deploy' to apply."
        )


@record_app.command("remove")
def record_remove(
    domain: Annotated[str, typer.Argument()],
    name: Annotated[str, typer.Argument()],
    type_: Annotated[RecordType, typer.Option("--type")] = RecordType.A,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Delete one record.

    Deleting the last record routed to a site withdraws its virtual host at the
    next deploy, along with any certificate BlitzeCDN manages for it — but the
    site and its settings stay. To take a hostname off the edge and keep
    answering for it, use 'record unroute' instead.
    """
    label = f"{name}.{domain}" if name != "@" else domain
    if not yes and not typer.confirm(f"Delete {type_.value} record for {label!r}?"):
        raise typer.Abort()
    common.control_plane().dns.delete_record(domain, name, type_, "cli")
    typer.echo(f"Deleted {label}")


# -- Export --------------------------------------------------------------


@dns_app.command("export")
def dns_export(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Emit every record for the system that publishes DNS.

    Records routed to a site carry no address: they must resolve to an edge,
    and edge addressing is owned by the DNS system rather than the control
    plane. The site name is reported instead so the two can be reconciled.
    """
    common.emit(common.control_plane().dns.dns_export(), json_output=json_output)


__all__ = ["dns_app", "domain_app", "record_app"]


# -- Rules ---------------------------------------------------------------


rule_app = typer.Typer(
    no_args_is_help=True,
    help="Override a zone's policy for some of its hostnames.",
)

_SET_HELP = (
    "A zone setting to override, as name=value. Repeatable. Values are read "
    "as JSON when they parse as JSON and as text otherwise, so "
    "cache_enabled=false is a boolean and cache_valid_success=10m is a string."
)


def _parse_overrides(pairs: list[str]) -> dict[str, Any]:
    """``name=value`` into the mapping a rule stores.

    JSON first so that ``false``, ``0`` and ``null`` arrive as themselves
    rather than as the strings that spell them; plain text otherwise, because
    every duration and hostname a setting takes would need quoting if not.
    """
    overrides: dict[str, Any] = {}
    for pair in pairs:
        name, separator, value = pair.partition("=")
        if not separator:
            raise typer.BadParameter(f"expected name=value, got {pair!r}")
        try:
            overrides[name.strip()] = json.loads(value)
        except json.JSONDecodeError:
            overrides[name.strip()] = value
    return overrides


@rule_app.command("add")
def rule_add(
    domain: Annotated[str, typer.Argument(help="Zone the rule belongs to.")],
    name: Annotated[str, typer.Argument(help="Name for the rule within the zone.")],
    set_: Annotated[list[str], typer.Option("--set", help=_SET_HELP)],
    match: Annotated[
        str,
        typer.Option(
            "--match",
            help="Hostname this covers: '*', an exact name, or '*.suffix'.",
        ),
    ] = "*",
    priority: Annotated[
        int,
        typer.Option("--priority", help="Lower runs first. The first match wins."),
    ] = 100,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Override some of a zone's policy for the hostnames a rule matches."""
    common.emit(
        common.control_plane().rules.create_rule(
            Rule(
                domain=domain,
                name=name,
                match=match,
                priority=priority,
                overrides=_parse_overrides(set_),
            ),
            "cli",
        ),
        json_output=json_output,
    )


@rule_app.command("list")
def rule_list(
    domain: Annotated[str, typer.Argument(help="Zone to list the rules of.")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List a zone's rules in the order a hostname is matched against them."""
    common.emit(
        common.control_plane().rules.list_rules(domain), json_output=json_output
    )


@rule_app.command("show")
def rule_show(
    domain: str,
    name: str,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show one rule: what it matches, and what it changes."""
    common.emit(
        common.control_plane().rules.get_rule(domain, name), json_output=json_output
    )


@rule_app.command("set")
def rule_set(
    domain: str,
    name: str,
    set_: Annotated[list[str] | None, typer.Option("--set", help=_SET_HELP)] = None,
    match: Annotated[str | None, typer.Option("--match")] = None,
    priority: Annotated[int | None, typer.Option("--priority")] = None,
    enabled: Annotated[bool | None, typer.Option("--enabled/--disabled")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Change a rule. Any --set replaces every override the rule had.

    Replaced rather than merged so that a setting can be taken back off a rule:
    if --set were additive there would be no way to stop overriding one.
    """
    changes: dict[str, Any] = {}
    if set_ is not None:
        changes["overrides"] = _parse_overrides(set_)
    if match is not None:
        changes["match"] = match
    if priority is not None:
        changes["priority"] = priority
    if enabled is not None:
        changes["enabled"] = enabled
    if not changes:
        raise typer.BadParameter(
            "nothing to change; pass --set, --match, or --priority"
        )
    common.emit(
        common.control_plane().rules.update_rule(
            domain, name, RulePatch.model_validate(changes), "cli"
        ),
        json_output=json_output,
    )


@rule_app.command("remove")
def rule_remove(
    domain: str,
    name: str,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Remove a rule. The hostnames it covered fall back to the zone's policy."""
    if not yes and not typer.confirm(f"Delete rule {name!r} in {domain!r}?"):
        raise typer.Abort()
    common.control_plane().rules.delete_rule(domain, name, "cli")
    typer.echo(f"Deleted rule {name} in {domain}")
