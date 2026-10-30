"""The `compression` contract's switch on one zone."""

from __future__ import annotations

from typing import Annotated

import typer

from blitzecdn.capabilities.compression.contract import (
    CompressionMode,
    CompressionPolicy,
)
from blitzecdn.capabilities.dns.domain import DomainPatch
from blitzecdn.entrypoints.cli import common
from blitzecdn.entrypoints.cli.domain_editing import report, update

#: Joined to `blitzecdn domain`, which dns founds: these edit the zone's
#: compression settings, so they ship with the capability that owns them.
domain_commands = typer.Typer()


@domain_commands.command("compression")
def domain_compression(
    name: Annotated[str, typer.Argument(help="Zone, e.g. example.com.")],
    mode: Annotated[
        CompressionMode,
        typer.Option(
            "--mode",
            help="Compress at the edge with Brotli and gzip, gzip only, or not at all.",
        ),
    ],
    json_output: common.JsonOutput = False,
) -> None:
    """Choose which encodings the edge produces for this zone.

    'brotli' offers Brotli to clients that accept it and gzip to the rest, and
    falls back to gzip on an edge without the Brotli module. 'off' stops the
    edge compressing; a response the origin already compressed is still passed
    through, because nginx never re-encodes an encoded body.
    """
    zone = update(name, DomainPatch.model_validate({"compression": mode}))
    report(
        zone,
        f"{zone.name} edge compression is now "
        f"{zone.policy[CompressionPolicy].compression.value!r}.",
        json_output=json_output,
    )
