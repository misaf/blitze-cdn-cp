"""`origin check` — what the edges can reach, answered by the edges."""

from __future__ import annotations

from typing import Annotated

import typer

from blitzecdn.cli import common
from blitzecdn.cli.common import ExitCode
from blitzecdn_origins.composition import build_origin_check_service

origin_app = typer.Typer(
    no_args_is_help=True, help="Check the origins the edges proxy to."
)


@origin_app.command("check")
def origin_check(
    limit: Annotated[str | None, common.LIMIT_OPTION] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Ask the edges to connect to the origins they proxy to.

    Catches the misconfigurations `validate` cannot see — an origin that does
    not resolve, is not listening, or presents a certificate that does not
    match the SNI the edge sends. Exits 3 if any edge could not reach one.

    The edges answer, not the controller: they are the machines that carry the
    traffic, and an origin that allow-lists them refuses the controller while
    working perfectly. Each failing site is reported with the edges that could
    not reach it — an origin no edge can reach is down, while one only some can
    reach is a routing or allow-list problem.
    """
    report = build_origin_check_service(common.control_plane()).check_origins(
        "cli", host_limit=limit
    )
    common.emit(report, json_output=json_output)
    if json_output:
        if report.failing_sites:
            raise typer.Exit(ExitCode.CONFIGURATION)
        return
    if not report.reporting:
        typer.echo("No edge reported an origin check.")
    elif not report.failing_sites:
        checked = sum(len(edge.checks) for edge in report.reporting)
        typer.echo(
            f"\n{checked} origin check(s) across "
            f"{len(report.reporting)} edge(s) answered as expected."
        )
    for edge in report.silent:
        typer.echo(f"\n{edge.host}: {edge.error}", err=True)
    for site, hosts in report.failing_sites.items():
        typer.echo(f"\n{site}: unreachable from {', '.join(hosts)}", err=True)
    if report.failing_sites:
        raise typer.Exit(ExitCode.CONFIGURATION)


__all__ = ["origin_app"]
