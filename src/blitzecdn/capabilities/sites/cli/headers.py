"""What the edge tells the origin about the visitor.

Edits the block declared by :mod:`blitzecdn.capabilities.sites.policy.headers`,
which this capability owns for the reason given there: writing the trusted
`BZ-*` headers is something a managed edge does with nothing installed beside
the control plane, so there is no distribution to reunite it with.
"""

from __future__ import annotations

from typing import Annotated

import typer

from blitzecdn.capabilities.sites.cli.app import _applied, site_app
from blitzecdn.capabilities.sites.domain import SitePatch
from blitzecdn.capabilities.sites.policy import SiteVisitorHeaders
from blitzecdn.cli import common


@site_app.command("visitor-headers")
def site_visitor_headers(
    name: Annotated[str, typer.Argument()],
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
    json_output: Annotated[bool, typer.Option("--json")] = False,
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

    BZ-IPCountry needs the 'geoip' capability on this controller — the
    blitzecdn-geoip distribution — and blitzecdn_geoip_enabled with a
    country database on the edge. A deploy refuses rather than send an origin a
    header that is silently absent.

    Options you do not name are left as they are.
    """
    control = common.control_plane()
    supplied = {"connecting_ip": connecting_ip, "ip_country": ip_country}
    named = {field: value for field, value in supplied.items() if value is not None}
    if not named:
        raise typer.BadParameter(
            "give at least one of --connecting-ip/--no-connecting-ip or "
            "--ip-country/--no-ip-country"
        )
    current = control.sites.get_site(name).visitor_headers
    headers = SiteVisitorHeaders.model_validate(current.model_dump() | named)
    site = control.site_editor.update_site(
        name, SitePatch(visitor_headers=headers), "cli"
    )
    common.emit(site, json_output=json_output)
    if not json_output:
        sent = [
            header
            for header, on in (
                ("BZ-Connecting-IP", site.visitor_headers.connecting_ip),
                ("BZ-IPCountry", site.visitor_headers.ip_country),
            )
            if on
        ]
        typer.echo(
            _applied(
                site,
                f"{site.name} now sends {', '.join(sent)} to the origin."
                if sent
                else f"{site.name} now sends no BZ-* visitor headers to the origin.",
            )
        )
