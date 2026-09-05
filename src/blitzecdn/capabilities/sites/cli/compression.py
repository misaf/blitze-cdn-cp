"""The `compression` contract\'s switch on one site.

Edits the field declared by :mod:`blitzecdn.capabilities.compression.policy`.
Whether an edge can actually produce Brotli is `blitzecdn-compression`\'s
question, asked at deploy time.
"""

from __future__ import annotations

from typing import Annotated

import typer

from blitzecdn.capabilities.compression.policy import CompressionMode
from blitzecdn.capabilities.sites.cli.app import _applied, _update, site_app
from blitzecdn.capabilities.sites.domain import SitePatch
from blitzecdn.cli import common


@site_app.command("compression")
def site_compression(
    name: Annotated[str, typer.Argument()],
    mode: Annotated[
        CompressionMode,
        typer.Option(
            "--mode",
            help="Compress at the edge with Brotli and gzip, gzip only, or not at all.",
        ),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Choose which encodings the edge produces for this site.

    'brotli' offers Brotli to clients that accept it and gzip to the rest, and
    falls back to gzip on an edge without the Brotli module. 'off' stops the
    edge compressing; a response the origin already compressed is still passed
    through, because nginx never re-encodes an encoded body.
    """
    site = _update(name, SitePatch(compression=mode))
    common.emit(site, json_output=json_output)
    if not json_output:
        typer.echo(
            _applied(
                site,
                f"{site.name} edge compression is now {site.compression.value!r}.",
            )
        )
