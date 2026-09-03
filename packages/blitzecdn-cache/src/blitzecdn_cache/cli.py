"""`cache` — remove cached responses from the edges."""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import urlsplit

import typer

from blitzecdn.cli import common
from blitzecdn.cli.common import ExitCode
from blitzecdn.capabilities.http.policy import HttpScheme
from blitzecdn_cache.composition import build_cache_service
from blitzecdn_cache.domain import CacheStatsReport, PurgeEntry

cache_app = typer.Typer(
    no_args_is_help=True, help="Purge cached responses from the edges."
)

#: `blitzecdn stats` is a root command, not `blitzecdn cache stats`.
#:
#: It reads as a verb an operator types directly and it was one before this
#: capability became a distribution, so it stays one — the registration
#: mechanism does not get to reshape the command tree. It lived in
#: `diagnostics` while `cache` was a package inside the control plane, which
#: put a command that reads this capability's report in a capability that had to
#: import it. The report is this package's, so the command is too.
stats_app = typer.Typer()


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
    result = build_cache_service(common.control_plane()).purge_cache(
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
    # Keep the caller's full request target here. CacheService knows the owning
    # site's policy and removes the query only when that site keys by path.
    uri = parsed.path or "/"
    if parsed.query:
        uri = f"{uri}?{parsed.query}"
    return PurgeEntry(host=parsed.hostname, uri=uri, scheme=HttpScheme(parsed.scheme))


@stats_app.command("stats")
def stats(
    limit: Annotated[str | None, common.LIMIT_OPTION] = None,
    by_site: Annotated[
        bool,
        typer.Option("--by-site", help="Break the numbers down by virtual host."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Report cache effectiveness across the edges.

    Reads the access log and nginx's own counters on each edge. Changes
    nothing, so it is safe to run at any time, including during a deploy.

    The hit ratio counts only requests that consulted the cache. Redirects,
    errors nginx served itself, and sites with caching disabled are excluded
    rather than scored as misses.
    """
    report = build_cache_service(common.control_plane()).cache_stats(
        "cli", host_limit=limit
    )
    if json_output:
        common.emit(_stats_document(report, by_site=by_site), json_output=True)
        return
    common.emit(_stats_document(report, by_site=by_site), json_output=False)
    for edge in report.silent:
        typer.echo(f"\n{edge.host} reported nothing: {edge.error}", err=True)
    if report.hit_ratio is None:
        typer.echo(
            "\nNo cacheable requests in the window read, so there is no hit "
            "ratio yet. A fresh edge or a quiet one both look like this."
        )


def _stats_document(report: CacheStatsReport, *, by_site: bool) -> dict[str, Any]:
    """One shape for both outputs, so --json is never a different answer."""
    document: dict[str, Any] = {
        "collected_at": report.collected_at.isoformat(),
        "hit_ratio": report.hit_ratio,
        "hits": report.hits,
        "cacheable_requests": report.cacheable_requests,
        "requests": report.requests,
        "edges": [
            {
                "host": edge.host,
                "hit_ratio": edge.hit_ratio,
                "requests": edge.requests,
                "nginx_reachable": edge.nginx_reachable,
                "connections": edge.connections,
                "error": edge.error,
            }
            for edge in report.edges
        ],
    }
    if by_site:
        document["sites"] = [
            {
                "site": site.site,
                "hit_ratio": site.hit_ratio,
                "hits": site.hits,
                "cacheable_requests": site.cacheable_requests,
                "requests": site.requests,
            }
            for site in report.by_site()
        ]
    return document
