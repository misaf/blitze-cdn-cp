"""`domain`, `record` and `dns` — the zone editor's command groups.

A record used to carry the whole of a site's policy, and this module used to
carry the ten commands that set it. They are `site` commands now, over in the
capability that owns them; what is left here edits zones and records, and the
only thing a record says about the CDN is which site answers for its hostname.
"""

from __future__ import annotations

from typing import Annotated

import typer

from blitzecdn.capabilities.dns.domain import DnsRecord, Domain, RecordType
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
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Register a DNS zone delegated to BlitzeCDN."""
    common.emit(
        common.control_plane().dns.create_domain(Domain(name=name), "cli"),
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
