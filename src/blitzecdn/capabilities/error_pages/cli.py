"""The `error_pages` contract's switches: error pages and maintenance mode."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from blitzecdn.capabilities.dns import DomainPatch
from blitzecdn.capabilities.error_pages.contract import (
    ERROR_PAGE_STATUSES,
    ErrorPagesPolicy,
)
from blitzecdn.entrypoints.cli import common
from blitzecdn.entrypoints.cli.domain_editing import report, update

#: Joined to `blitzecdn domain`, which dns founds: these edit the zone's
#: error presentation, so they ship with the capability that owns them.
domain_commands = typer.Typer()


@domain_commands.command("error-pages")
def domain_error_pages(
    name: Annotated[str, typer.Argument(help="Zone, e.g. example.com.")],
    page: Annotated[
        list[str] | None,
        typer.Option(
            "--page",
            help=(
                "STATUS=FILE, an HTML file served in place of that error, e.g. "
                "502=./502.html. Statuses: "
                + ", ".join(ERROR_PAGE_STATUSES)
                + ". Repeat for more; replaces the current pages."
            ),
        ),
    ] = None,
    clear: Annotated[
        bool,
        typer.Option("--clear", help="Forget every page; visitors see Nginx's own."),
    ] = False,
    json_output: common.JsonOutput = False,
) -> None:
    """Brand the pages visitors see when this zone's sites fail.

    A 5xx page replaces the origin's own error as well as the edge's when the
    origin cannot be reached or times out, and the status is kept. A 403 or
    429 page replaces only what the edge's firewall and rate limit answer; an
    origin's own 403 or 429 still reaches visitors as it was sent.

    Pages ship with the configuration, up to 64 KiB each. Keep them
    self-contained, with inline styles and images: the origin may be what is
    down.
    """
    if page and clear:
        raise typer.BadParameter("give --page or --clear, not both")
    if not page and not clear:
        raise typer.BadParameter("give at least one --page, or --clear")
    pages = {} if clear else _pages(page or [])
    zone = update(name, DomainPatch.model_validate({"error_pages": pages}))
    statuses = sorted(zone.policy[ErrorPagesPolicy].error_pages)
    state = (
        "has custom error pages for " + ", ".join(statuses)
        if statuses
        else "shows Nginx's own error pages"
    )
    report(zone, f"{zone.name} now {state}.", json_output=json_output)


@domain_commands.command("maintenance")
def domain_maintenance(
    name: Annotated[str, typer.Argument(help="Zone, e.g. example.com.")],
    on: Annotated[
        bool | None,
        typer.Option(
            "--on/--off",
            help="Answer every visitor with the maintenance page, or stop.",
        ),
    ] = None,
    page: Annotated[
        Path | None,
        typer.Option(
            "--page",
            help="HTML file served in maintenance mode, up to 64 KiB.",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
    default_page: Annotated[
        bool,
        typer.Option(
            "--default-page",
            help="Forget --page: use the 503 error page, else a built-in one.",
        ),
    ] = False,
    retry_after: Annotated[
        int | None,
        typer.Option(
            "--retry-after",
            help="Seconds sent as Retry-After while in maintenance; 300 by default.",
        ),
    ] = None,
    json_output: common.JsonOutput = False,
) -> None:
    """Put this zone's sites into maintenance mode, or take them out.

    In maintenance the edge answers every request with the maintenance page,
    status 503 and Retry-After, and the origin is not contacted. Networks in
    the zone's firewall allow-sources are exempt and reach the origin as
    usual, so the owner can check the site before turning maintenance off.

    Options you do not name are left as they are, so a page can be set before
    maintenance is turned on.
    """
    if page is not None and default_page:
        raise typer.BadParameter("give --page or --default-page, not both")
    supplied: dict[str, object] = {
        "maintenance_mode": on,
        "maintenance_page": None if page is None else _read(page),
        "maintenance_retry_after": retry_after,
    }
    named = {field: value for field, value in supplied.items() if value is not None}
    if default_page:
        named["maintenance_page"] = None
    if not named:
        raise typer.BadParameter(
            "give at least one of --on/--off, --page, --default-page or --retry-after"
        )
    zone = update(name, DomainPatch.model_validate(named))
    policy = zone.policy[ErrorPagesPolicy]
    state = (
        f"in maintenance, retry after {policy.maintenance_retry_after}s"
        if policy.maintenance_mode
        else "not in maintenance"
    )
    report(zone, f"{zone.name} is now {state}.", json_output=json_output)


def _pages(values: list[str]) -> dict[str, str]:
    """Read repeated ``STATUS=FILE`` options; the contract validates the pages."""
    pages: dict[str, str] = {}
    for value in values:
        status, separator, path = value.partition("=")
        if not separator or not status.strip() or not path.strip():
            raise typer.BadParameter(f"{value!r} is not STATUS=FILE, e.g. 502=502.html")
        pages[status.strip()] = _read(Path(path.strip()))
    return pages


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise typer.BadParameter(
            f"cannot read {path} as UTF-8 text: {error}"
        ) from error
