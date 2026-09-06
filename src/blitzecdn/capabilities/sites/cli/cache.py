"""The `cache` contract\'s switches on one site.

Edits fields declared by :mod:`blitzecdn.capabilities.cache.policy`. Purging
and cache statistics are operations rather than site policy, so they arrive
with `blitzecdn-cache` and are not here.
"""

from __future__ import annotations

from typing import Annotated

import typer

from blitzecdn.capabilities.cache.policy import CacheQueryStringMode
from blitzecdn.capabilities.sites.cli.app import _applied, _update, site_app
from blitzecdn.capabilities.sites.domain import SitePatch
from blitzecdn.cli import common


@site_app.command("cache")
def site_cache(
    name: Annotated[str, typer.Argument()],
    on: Annotated[
        bool | None,
        typer.Option("--on/--off", help="Cache this site's responses, or stop."),
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
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Choose whether this site is cached, and for how long.

    A site is cached by default, so `--off` is the opt-out; turning caching off
    also withdraws the site's claim on the 'cache' capability, which is what
    lets a controller without blitzecdn-cache converge it. Turning it back on
    means a deploy will refuse by name until that distribution is attached,
    rather than quietly serving everything from the origin.

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
    # Built from the dict rather than by keyword: a patch applies the fields
    # that were *set*, so passing `cache_enabled=None` for an option nobody
    # named would clear the switch rather than leave it alone.
    site = _update(name, SitePatch.model_validate(named))
    common.emit(site, json_output=json_output)
    if not json_output:
        state = (
            f"caching 2xx for {site.cache_valid_success} and 404 for "
            f"{site.cache_valid_not_found}"
            if site.cache_enabled
            else "not caching"
        )
        typer.echo(_applied(site, f"{site.name} is now {state}."))


@site_app.command("cache-query-string")
def site_cache_query_string(
    name: Annotated[str, typer.Argument()],
    mode: Annotated[
        CacheQueryStringMode,
        typer.Option(
            "--mode", help="Include query strings in cache keys, or ignore them."
        ),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Choose whether query strings distinguish cached responses."""
    site = _update(name, SitePatch(cache_query_string_mode=mode))
    common.emit(site, json_output=json_output)
    if not json_output:
        typer.echo(
            _applied(
                site,
                f"{site.name} cache query-string mode is now "
                f"{site.cache_query_string_mode.value!r}.",
            )
        )
