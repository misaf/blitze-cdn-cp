"""The `http` contract\'s switches: protocols, upload limit, scheme redirect.

Edits fields declared by :mod:`blitzecdn.capabilities.http.policy`. `http3` is
the switch\'s value on the zone document; whether an edge can serve QUIC at all
is `blitzecdn-http3`\'s question, asked at deploy time.
"""

from __future__ import annotations

from typing import Annotated

import typer

from blitzecdn.capabilities.dns.cli.app import _applied, _update, domain_app
from blitzecdn.capabilities.dns.domain import DomainPatch
from blitzecdn.capabilities.http.policy import MaxUploadSize
from blitzecdn.cli import common


@domain_app.command("http3")
def domain_http3(
    name: Annotated[str, typer.Argument(help="Zone, e.g. example.com.")],
    on: Annotated[
        bool,
        typer.Option(
            "--on/--off", help="Offer HTTP/3 over QUIC on UDP/443, or withdraw it."
        ),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Enable or disable visitor HTTP/3 for one TLS-enabled zone.

    HTTP/2 and HTTP/1.1 remain available over TCP. This setting never changes
    the protocol used from the edge to the origin.
    """
    zone = _update(name, DomainPatch(http3_enabled=on))
    common.emit(zone, json_output=json_output)
    if not json_output:
        typer.echo(
            _applied(
                zone,
                f"HTTP/3 is now {'enabled' if on else 'disabled'} for {zone.name}.",
            )
        )


@domain_app.command("max-upload-size")
def domain_max_upload_size(
    name: Annotated[str, typer.Argument(help="Zone, e.g. example.com.")],
    size: Annotated[
        MaxUploadSize,
        typer.Option("--size", help="Largest visitor request body this zone accepts."),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Set the largest request body this zone accepts from a visitor.

    A larger body is refused at the edge with 413 before the origin is
    contacted, so this is a limit on what visitors may upload rather than on
    what the origin is willing to receive.
    """
    zone = _update(name, DomainPatch(max_upload_size=size))
    common.emit(zone, json_output=json_output)
    if not json_output:
        typer.echo(
            _applied(zone, f"{zone.name} now accepts uploads up to {size.value}.")
        )


@domain_app.command("always-use-https")
def domain_always_use_https(
    name: Annotated[str, typer.Argument(help="Zone, e.g. example.com.")],
    on: Annotated[
        bool,
        typer.Option(
            "--on/--off",
            help="Redirect all visitor HTTP requests to HTTPS, or serve both schemes.",
        ),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Enable or disable the HTTP-to-HTTPS redirect for one zone.

    The setting takes effect only while the SSL mode serves HTTPS. Disabling it
    leaves HTTPS available and serves HTTP requests through to the origin.
    """
    zone = _update(name, DomainPatch(always_use_https=on))
    common.emit(zone, json_output=json_output)
    if not json_output:
        typer.echo(
            _applied(
                zone,
                f"Always Use HTTPS is now {'enabled' if on else 'disabled'} "
                f"for {zone.name}.",
            )
        )
