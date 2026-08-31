"""The root Typer application and the global options.

Kept apart from the command groups so each of them can import ``app`` without
importing the others.
"""

from __future__ import annotations

from typing import Annotated

import typer

from blitzecdn import __version__
from blitzecdn.core.logging import configure_logging

app = typer.Typer(
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    help="""Securely manage BlitzeCDN edge desired state.

    You do not create virtual hosts. Register a zone with 'domain add', add a
    record with 'record add --proxied', and the edge site is derived from it —
    unproxy the record and the site is gone. Per-hostname settings (origin,
    caching, TLS, firewall) live on the record.

    Nothing reaches an edge until 'deploy'. Commands change local desired state
    only, so you can stage a batch of edits and apply them in one converge.
    Preview with 'plan', apply with 'deploy', undo with 'rollback', and check
    the fleet still matches with 'drift'.
    """,
)


def _version_callback(value: bool) -> None:
    """Print the package version."""
    if not value:
        return
    typer.echo(f"blitzecdn {__version__}")
    raise typer.Exit()


@app.callback()
def main(
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Enable diagnostic logging.")
    ] = False,
    log_json: Annotated[bool, typer.Option(help="Write logs as JSON.")] = False,
    _version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the control plane, edge collection, and schema versions.",
        ),
    ] = False,
) -> None:
    configure_logging(verbose=verbose, json_output=log_json)
