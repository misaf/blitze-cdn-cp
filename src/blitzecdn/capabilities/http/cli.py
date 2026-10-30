"""The `http` contract's switches: protocols, upload limit, scheme redirect."""

from __future__ import annotations

from typing import Annotated

import typer

from blitzecdn.capabilities.dns.domain import DomainPatch
from blitzecdn.capabilities.http.contract import MaxUploadSize
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
