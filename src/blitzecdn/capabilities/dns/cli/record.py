"""Records: what DNS answers with, and whether the edge answers instead.

A record used to carry the whole of a site's policy, and this module used to
carry the ten commands that set it. Those are `domain` commands now, beside the
zone that holds the policy; what is left here is the record and its one switch.
"""

from __future__ import annotations

from typing import Annotated

import typer

from blitzecdn.capabilities.dns.cli.app import dns_app, record_app
from blitzecdn.capabilities.dns.domain import DnsRecord, RecordType
from blitzecdn.cli import common


@record_app.command("add")
def record_add(
    domain: Annotated[str, typer.Argument(help="Zone the record belongs to.")],
    name: Annotated[str, typer.Argument(help="Subdomain label, '@', or '*'.")],
    value: Annotated[
        str | None,
        typer.Option(
            "--value",
            help="Address to answer with. Implies --no-proxy; bypasses the CDN.",
        ),
    ] = None,
    proxied: Annotated[
        bool,
        typer.Option("--proxy/--no-proxy", help="Whether the edge serves this name."),
    ] = True,
    type_: Annotated[RecordType, typer.Option("--type")] = RecordType.A,
    ttl: Annotated[int, typer.Option("--ttl")] = 300,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Add a DNS record, served by the edge or answering with an address.

    Proxied by default, which is the reason to put a hostname in a CDN at all.
    How it is served is the zone's policy, bent by whichever rule matches it —
    there is no second object to create first.

    With --value the record bypasses the CDN and resolves straight to that
    address; passing one implies --no-proxy, since a proxied record is answered
    with an edge address rather than one you supply.

    A dual-stack hostname is two records, one A and one AAAA. Both are proxied
    or neither is, and being one virtual host is no longer something they have
    to agree about: neither of them carries a policy.
    """
    record = DnsRecord(
        domain=domain,
        name=name,
        type=type_,
        value=value,
        ttl=ttl,
        proxied=proxied and value is None,
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

    Shows every zone unless you name one. A proxied record carries no address:
    it is answered with the fleet's. The policy behind it is 'blitzecdn domain
    show', and any exception to that is 'blitzecdn rule list'.
    """
    common.emit(
        common.control_plane().dns.list_records(domain), json_output=json_output
    )


@record_app.command("proxy")
def record_proxy(
    domain: Annotated[str, typer.Argument()],
    name: Annotated[str, typer.Argument()],
    type_: Annotated[RecordType, typer.Option("--type")] = RecordType.A,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Put a hostname on the edge, served by its zone's policy.

    Takes effect on the edge at the next deploy. It only reaches clients once
    DNS answers with an edge address, which the DNS system owns. Any address
    the record was answering with is cleared, because a proxied record is
    answered with the fleet's.
    """
    record = common.control_plane().dns.proxy(domain, name, type_, "cli")
    common.emit(record, json_output=json_output)
    if not json_output:
        typer.echo(
            f"{record.fqdn} is now served by the edge. Run 'blitzecdn deploy' "
            "to apply, and make sure DNS points at an edge."
        )


@record_app.command("unproxy")
def record_unproxy(
    domain: Annotated[str, typer.Argument()],
    name: Annotated[str, typer.Argument()],
    value: Annotated[
        str, typer.Option("--value", help="Address DNS should answer with instead.")
    ],
    type_: Annotated[RecordType, typer.Option("--type")] = RecordType.A,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Take a hostname off the edge, answering with an address instead.

    The address is required rather than inferred from the origin. That is
    deliberate: answering with the origin address is what publishes an origin
    the CDN existed to keep private, and it should be a thing you asked for
    rather than a default.

    The zone's policy is left as it is. If nothing else in the zone is proxied,
    it simply stops being served.
    """
    record = common.control_plane().dns.unproxy(domain, name, type_, value, "cli")
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
