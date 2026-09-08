"""The `compression` contract\'s switch on one zone.

Edits the field declared by :mod:`blitzecdn.capabilities.compression.policy`.
Whether an edge can actually produce Brotli is `blitzecdn-compression`\'s
question, asked at deploy time.
"""

from __future__ import annotations

from typing import Annotated

import typer

from blitzecdn.capabilities.compression.policy import CompressionMode
from blitzecdn.capabilities.dns.cli.app import _applied, _update, domain_app
from blitzecdn.capabilities.dns.domain import DomainPatch
from blitzecdn.cli import common


@domain_app.command("compression")
def domain_compression(
    name: Annotated[str, typer.Argument(help="Zone, e.g. example.com.")],
    mode: Annotated[
        CompressionMode,
        typer.Option(
            "--mode",
            help="Compress at the edge with Brotli and gzip, gzip only, or not at all.",
        ),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Choose which encodings the edge produces for this zone.

    'brotli' offers Brotli to clients that accept it and gzip to the rest, and
    falls back to gzip on an edge without the Brotli module. 'off' stops the
    edge compressing; a response the origin already compressed is still passed
    through, because nginx never re-encodes an encoded body.
    """
    zone = _update(name, DomainPatch(compression=mode))
    common.emit(zone, json_output=json_output)
    if not json_output:
        typer.echo(
            _applied(
                zone,
                f"{zone.name} edge compression is now {zone.compression.value!r}.",
            )
        )
