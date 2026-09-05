"""The `http` contract\'s switches: protocols, upload limit, scheme redirect.

Edits fields declared by :mod:`blitzecdn.capabilities.http.policy`. `http3` is
the switch\'s value on the site document; whether an edge can serve QUIC at all
is `blitzecdn-http3`\'s question, asked at deploy time.
"""

from __future__ import annotations

from typing import Annotated

import typer

from blitzecdn.capabilities.http.policy import MaxUploadSize
from blitzecdn.capabilities.sites.cli.app import _applied, _update, site_app
from blitzecdn.capabilities.sites.domain import SitePatch
from blitzecdn.cli import common


@site_app.command("http3")
def site_http3(
    name: Annotated[str, typer.Argument()],
    on: Annotated[
        bool,
        typer.Option(
            "--on/--off", help="Offer HTTP/3 over QUIC on UDP/443, or withdraw it."
        ),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Enable or disable visitor HTTP/3 for one TLS-enabled site.

    HTTP/2 and HTTP/1.1 remain available over TCP. This setting never changes
    the protocol used from the edge to the origin.
    """
    site = _update(name, SitePatch(http3_enabled=on))
    common.emit(site, json_output=json_output)
    if not json_output:
        typer.echo(
            _applied(
                site,
                f"HTTP/3 is now {'enabled' if on else 'disabled'} for {site.name}.",
            )
        )


@site_app.command("max-upload-size")
def site_max_upload_size(
    name: Annotated[str, typer.Argument()],
    size: Annotated[
        MaxUploadSize,
        typer.Option("--size", help="Largest visitor request body this site accepts."),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Set the largest request body this site accepts from a visitor.

    A larger body is refused at the edge with 413 before the origin is
    contacted, so this is a limit on what visitors may upload rather than on
    what the origin is willing to receive.
    """
    site = _update(name, SitePatch(max_upload_size=size))
    common.emit(site, json_output=json_output)
    if not json_output:
        typer.echo(
            _applied(site, f"{site.name} now accepts uploads up to {size.value}.")
        )


@site_app.command("always-use-https")
def site_always_use_https(
    name: Annotated[str, typer.Argument()],
    on: Annotated[
        bool,
        typer.Option(
            "--on/--off",
            help="Redirect all visitor HTTP requests to HTTPS, or serve both schemes.",
        ),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Enable or disable the HTTP-to-HTTPS redirect for one site.

    The setting takes effect only while the SSL mode serves HTTPS. Disabling it
    leaves HTTPS available and serves HTTP requests through to the origin.
    """
    site = _update(name, SitePatch(always_use_https=on))
    common.emit(site, json_output=json_output)
    if not json_output:
        typer.echo(
            _applied(
                site,
                f"Always Use HTTPS is now {'enabled' if on else 'disabled'} "
                f"for {site.name}.",
            )
        )
