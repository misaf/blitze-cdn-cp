"""HTTP protocols, upload limits, redirects and visitor headers."""

from __future__ import annotations

from typing import Annotated

import typer

from blitzecdn.capabilities.dns import DnsService
from blitzecdn.capabilities.dns.domain import DomainPatch
from blitzecdn.capabilities.http.contract import HeaderPolicy, MaxUploadSize
from blitzecdn.capabilities.http.domain import EditVisitorHeaders
from blitzecdn.entrypoints.cli import common
from blitzecdn.entrypoints.cli.domain_editing import report, update

#: Joined to `blitzecdn domain`, which dns founds: these edit the zone's
#: http settings, so they ship with the capability that owns them.
domain_commands = typer.Typer()


@domain_commands.command("http3")
def domain_http3(
    name: Annotated[str, typer.Argument(help="Zone, e.g. example.com.")],
    on: Annotated[
        bool,
        typer.Option(
            "--on/--off", help="Offer HTTP/3 over QUIC on UDP/443, or withdraw it."
        ),
    ],
    json_output: common.JsonOutput = False,
) -> None:
    """Enable or disable visitor HTTP/3 for one TLS-enabled zone.

    HTTP/2 and HTTP/1.1 remain available over TCP. This setting never changes
    the protocol used from the edge to the origin.
    """
    zone = update(name, DomainPatch.model_validate({"http3_enabled": on}))
    report(
        zone,
        f"HTTP/3 is now {'enabled' if on else 'disabled'} for {zone.name}.",
        json_output=json_output,
    )


@domain_commands.command("max-upload-size")
def domain_max_upload_size(
    name: Annotated[str, typer.Argument(help="Zone, e.g. example.com.")],
    size: Annotated[
        MaxUploadSize,
        typer.Option("--size", help="Largest visitor request body this zone accepts."),
    ],
    json_output: common.JsonOutput = False,
) -> None:
    """Set the largest request body this zone accepts from a visitor.

    A larger body is refused at the edge with 413 before the origin is
    contacted, so this is a limit on what visitors may upload rather than on
    what the origin is willing to receive.
    """
    zone = update(name, DomainPatch.model_validate({"max_upload_size": size}))
    report(
        zone,
        f"{zone.name} now accepts uploads up to {size.value}.",
        json_output=json_output,
    )


@domain_commands.command("always-use-https")
def domain_always_use_https(
    name: Annotated[str, typer.Argument(help="Zone, e.g. example.com.")],
    on: Annotated[
        bool,
        typer.Option(
            "--on/--off",
            help="Redirect all visitor HTTP requests to HTTPS, or serve both schemes.",
        ),
    ],
    json_output: common.JsonOutput = False,
) -> None:
    """Enable or disable the HTTP-to-HTTPS redirect for one zone.

    The setting takes effect only while the SSL mode serves HTTPS. Disabling it
    leaves HTTPS available and serves HTTP requests through to the origin.
    """
    zone = update(name, DomainPatch.model_validate({"always_use_https": on}))
    report(
        zone,
        f"Always Use HTTPS is now {'enabled' if on else 'disabled'} for {zone.name}.",
        json_output=json_output,
    )


@domain_commands.command("visitor-headers")
def domain_visitor_headers(
    name: Annotated[str, typer.Argument(help="Zone, e.g. example.com.")],
    connecting_ip: Annotated[
        bool | None,
        typer.Option(
            "--connecting-ip/--no-connecting-ip",
            help="Send BZ-Connecting-IP, the visitor address the edge saw.",
        ),
    ] = None,
    ip_country: Annotated[
        bool | None,
        typer.Option(
            "--ip-country/--no-ip-country",
            help="Send BZ-IPCountry, the visitor's ISO 3166-1 alpha-2 code. "
            "Needs GeoIP on the edge.",
        ),
    ] = None,
    json_output: common.JsonOutput = False,
) -> None:
    """Choose what the edge tells the origin about the visitor.

    BZ-Connecting-IP carries the visitor address, IPv4 or IPv6, as nginx saw it
    on the connection; it is on by default, because an origin behind the CDN
    sees an edge address on every connection. BZ-IPCountry carries the country
    the GeoIP2 database resolves that address to, and is off by default.

    Both are written by BlitzeCDN and overwrite whatever the visitor sent under
    the same name; a header turned off here is cleared rather than forwarded,
    so the BZ- namespace never carries client input. Trust them at the origin
    only where the origin accepts traffic from BlitzeCDN edges alone.

    BZ-IPCountry needs the 'geoip' capability, and blitzecdn_geoip_enabled
    with a country database on the edge. A deploy refuses rather than send an origin a
    header that is silently absent.

    Options you do not name are left as they are.
    """
    supplied = {"connecting_ip": connecting_ip, "ip_country": ip_country}
    named = {field: value for field, value in supplied.items() if value is not None}
    if not named:
        raise typer.BadParameter(
            "give at least one of --connecting-ip/--no-connecting-ip or "
            "--ip-country/--no-ip-country"
        )
    zone = common.service(DnsService).update_domain(
        name, EditVisitorHeaders.model_validate(named), "cli"
    )
    sent = [
        header
        for header, on in (
            (
                "BZ-Connecting-IP",
                zone.policy[HeaderPolicy].visitor_headers.connecting_ip,
            ),
            ("BZ-IPCountry", zone.policy[HeaderPolicy].visitor_headers.ip_country),
        )
        if on
    ]
    report(
        zone,
        f"{zone.name} now sends {', '.join(sent)} to the origin."
        if sent
        else f"{zone.name} now sends no BZ-* visitor headers to the origin.",
        json_output=json_output,
    )
