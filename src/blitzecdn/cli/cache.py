"""`cache` — remove cached responses from the edges."""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlsplit

import typer

from blitzecdn.cli import common
from blitzecdn.cli.common import ExitCode
from blitzecdn.domain.cache import PurgeEntry
from blitzecdn.domain.sites import HttpScheme

cache_app = typer.Typer(
    no_args_is_help=True, help="Purge cached responses from the edges."
)


@cache_app.command("purge")
def cache_purge(
    url: Annotated[
        list[str] | None,
        typer.Option(
            "--url",
            help=(
                "Absolute URL to purge, e.g. https://cdn.example.com/app.js. "
                "Repeat the option to purge more than one."
            ),
        ),
    ] = None,
    everything: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Empty the cache entirely instead of removing named URLs.",
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Required confirmation for --all."),
    ] = False,
    limit: Annotated[str | None, common.LIMIT_OPTION] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Remove cached responses from the edges.

    Every variant of a URL goes: nginx keys entries by method and by content
    encoding, so one URL is several entries and leaving any behind would keep
    serving the object to some clients.

    Purging changes no configuration and writes no desired state, so it needs
    no deploy afterwards and does not wait for one in progress.
    """
    if (
        everything
        and not yes
        and not typer.confirm(
            "Empty the cache on "
            + (f"edges matching {limit!r}" if limit else "every edge")
            + "? Every object will be re-fetched from its origin."
        )
    ):
        raise typer.Abort()
    entries = [_purge_entry(value) for value in url or []]
    result = common.control_plane().cache.purge_cache(
        "cli", entries=entries, purge_all=everything, host_limit=limit
    )
    common.emit(result, json_output=json_output)
    if not json_output:
        if result.complete:
            typer.echo(f"\nPurged on all {len(result.hosts)} edges.")
        for host in result.failed:
            typer.echo(
                f"\n{host.host} did not purge, so it may still be serving the "
                "cached copy. Re-run once it is reachable.",
                err=True,
            )
    if not result.complete:
        raise typer.Exit(ExitCode.DEPLOYMENT_FAILED)


def _purge_entry(value: str) -> PurgeEntry:
    """Read an operator-supplied URL into the host and path the cache keys on."""
    parsed = urlsplit(value if "//" in value else f"https://{value}")
    if not parsed.hostname:
        raise typer.BadParameter(f"{value!r} has no hostname")
    if parsed.scheme not in set(HttpScheme):
        raise typer.BadParameter(f"{value!r} must be http or https")
    # The query string is part of $request_uri and therefore part of the cache
    # key, so it is kept: purging '/a' must not silently purge '/a?v=2'.
    uri = parsed.path or "/"
    if parsed.query:
        uri = f"{uri}?{parsed.query}"
    return PurgeEntry(host=parsed.hostname, uri=uri, scheme=HttpScheme(parsed.scheme))
