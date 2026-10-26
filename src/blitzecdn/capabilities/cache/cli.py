"""`cache` purge and `stats`, plus the zone cache settings under `domain`."""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import urlsplit

import typer

from blitzecdn.capabilities.cache.contract import CacheQueryStringMode
from blitzecdn.capabilities.cache.domain import CacheStatsReport, PurgeEntry
from blitzecdn.capabilities.cache.service import CacheService
from blitzecdn.capabilities.dns.cli.editing import report, update
from blitzecdn.capabilities.dns.domain import DomainPatch
from blitzecdn.capabilities.http.contract import HttpScheme
from blitzecdn.entrypoints.cli import common
from blitzecdn.entrypoints.cli.common import ExitCode

cache_app = typer.Typer(
    no_args_is_help=True, help="Purge cached responses from the edges."
)

#: `blitzecdn stats` is a root verb, contributed by the capability whose report it
#: reads.
stats_app = typer.Typer()

#: Joined to `blitzecdn domain`, which dns founds.
domain_commands = typer.Typer()


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
    yes: common.Yes = False,
    limit: Annotated[str | None, common.LIMIT_OPTION] = None,
    json_output: common.JsonOutput = False,
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
    result = common.service(CacheService).purge_cache(
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
    json_output: common.JsonOutput = False,
) -> None:
    """Report cache effectiveness across the edges.

    Reads the access log and nginx's own counters on each edge. Changes
    nothing, so it is safe to run at any time, including during a deploy.

    The hit ratio counts only requests that consulted the cache. Redirects,
    errors nginx served itself, and sites with caching disabled are excluded
    rather than scored as misses.
    """
    report = common.service(CacheService).cache_stats("cli", host_limit=limit)
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


@domain_commands.command("cache")
def domain_cache(
    name: Annotated[str, typer.Argument(help="Zone, e.g. example.com.")],
    on: Annotated[
        bool | None,
        typer.Option("--on/--off", help="Cache this zone's responses, or stop."),
    ] = None,
    success: Annotated[
        str | None,
        typer.Option(
            "--success",
            help="How long a cached 2xx stays valid, e.g. 10m, 4h, 7d.",
        ),
    ] = None,
    not_found: Annotated[
        str | None,
        typer.Option(
            "--not-found",
            help="How long a cached 404 stays valid, e.g. 1m.",
        ),
    ] = None,
    json_output: common.JsonOutput = False,
) -> None:
    """Choose whether this zone is cached, and for how long.

    A zone is cached by default, so `--off` is the opt-out; turning caching off
    also withdraws the zone's claim on the 'cache' capability, which is what
    lets an edge without it converge the zone. Turning it back on means a
    deploy will refuse by name until the edges provide it, rather than quietly
    serving everything from the origin.

    The two durations are an integer and a unit — ms, s, m, h, d or w — and
    they are separate because a miss and a not-found cost the origin different
    amounts: a short `--not-found` keeps a newly published URL from staying
    missing, while a long `--success` is what the cache is for.

    Options you do not name are left as they are.
    """
    supplied = {
        "cache_enabled": on,
        "cache_valid_success": success,
        "cache_valid_not_found": not_found,
    }
    named = {field: value for field, value in supplied.items() if value is not None}
    if not named:
        raise typer.BadParameter(
            "give at least one of --on/--off, --success or --not-found"
        )
    # Built from the dict so an unnamed option stays unset rather than cleared.
    zone = update(name, DomainPatch.model_validate(named))
    state = (
        f"caching 2xx for {zone.policy.cache.cache_valid_success} and 404 for "
        f"{zone.policy.cache.cache_valid_not_found}"
        if zone.policy.cache.cache_enabled
        else "not caching"
    )
    report(zone, f"{zone.name} is now {state}.", json_output=json_output)


@domain_commands.command("cache-query-string")
def domain_cache_query_string(
    name: Annotated[str, typer.Argument(help="Zone, e.g. example.com.")],
    mode: Annotated[
        CacheQueryStringMode,
        typer.Option(
            "--mode", help="Include query strings in cache keys, or ignore them."
        ),
    ],
    json_output: common.JsonOutput = False,
) -> None:
    """Choose whether query strings distinguish cached responses."""
    zone = update(name, DomainPatch(cache_query_string_mode=mode))
    report(
        zone,
        f"{zone.name} cache query-string mode is now "
        f"{zone.policy.cache.cache_query_string_mode.value!r}.",
        json_output=json_output,
    )
