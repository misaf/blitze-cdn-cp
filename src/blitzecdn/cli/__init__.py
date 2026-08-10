"""The `blitzecdn` command line.

Importing this package is what assembles the CLI: each command module registers
its commands on the root ``app`` as a side effect of being imported, and the
sub-app registrations below are the one place the command tree is described.

``run`` is the console-script entry point (``blitzecdn = "blitzecdn.cli:run"``),
so it has to live here rather than in :mod:`blitzecdn.cli.app` — by the time it
is called every command module must already have been imported.
"""

from __future__ import annotations

import typer
from pydantic import ValidationError

from blitzecdn.cli import (
    bootstrap,
    cache,
    certs,
    common,
    deploy,
    diagnostics,
    edges,
    zones,
)
from blitzecdn.cli.app import app, main
from blitzecdn.cli.common import ExitCode, control_plane, emit, settings
from blitzecdn.config import Settings
from blitzecdn.exceptions import BlitzeError

app.add_typer(zones.site_app, name="site")
app.add_typer(edges.edge_app, name="edge")
app.add_typer(zones.domain_app, name="domain")
app.add_typer(zones.record_app, name="record")
app.add_typer(zones.dns_app, name="dns")
app.add_typer(certs.cert_app, name="cert")
app.add_typer(edges.origin_app, name="origin")
app.add_typer(cache.cache_app, name="cache")

__all__ = [
    "ExitCode",
    "Settings",
    "app",
    "bootstrap",
    "cache",
    "certs",
    "common",
    "control_plane",
    "deploy",
    "diagnostics",
    "edges",
    "emit",
    "main",
    "run",
    "settings",
    "zones",
]


def run() -> None:
    try:
        app()
    except (BlitzeError, ValidationError, OSError) as exc:
        typer.echo(f"error: {exc}", err=True)
        # SystemExit, not typer.Exit: we are outside Click's invocation by the
        # time app() has raised, so a typer.Exit here is nothing but an
        # unhandled exception and prints a traceback over the message above.
        raise SystemExit(ExitCode.INVALID_INPUT) from exc
