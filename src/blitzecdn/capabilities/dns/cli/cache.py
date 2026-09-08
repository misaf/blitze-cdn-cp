"""The `cache` contract\'s switches on one zone.

Edits fields declared by :mod:`blitzecdn.capabilities.cache.policy`. Purging
and cache statistics are operations rather than zone policy, so they arrive
with `blitzecdn-cache` and are not here.
"""

from __future__ import annotations

from typing import Annotated

import typer

from blitzecdn.capabilities.cache.policy import CacheQueryStringMode
from blitzecdn.capabilities.dns.cli.app import _applied, _update, domain_app
from blitzecdn.capabilities.dns.domain import DomainPatch
from blitzecdn.cli import common


@domain_app.command("cache")
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
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Choose whether this zone is cached, and for how long.

    A zone is cached by default, so `--off` is the opt-out; turning caching off
    also withdraws the zone's claim on the 'cache' capability, which is what
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
    zone = _update(name, DomainPatch.model_validate(named))
    common.emit(zone, json_output=json_output)
    if not json_output:
        state = (
            f"caching 2xx for {zone.cache_valid_success} and 404 for "
            f"{zone.cache_valid_not_found}"
            if zone.cache_enabled
            else "not caching"
        )
        typer.echo(_applied(zone, f"{zone.name} is now {state}."))


@domain_app.command("cache-query-string")
def domain_cache_query_string(
    name: Annotated[str, typer.Argument(help="Zone, e.g. example.com.")],
    mode: Annotated[
        CacheQueryStringMode,
        typer.Option(
            "--mode", help="Include query strings in cache keys, or ignore them."
        ),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Choose whether query strings distinguish cached responses."""
    zone = _update(name, DomainPatch(cache_query_string_mode=mode))
    common.emit(zone, json_output=json_output)
    if not json_output:
        typer.echo(
            _applied(
                zone,
                f"{zone.name} cache query-string mode is now "
                f"{zone.cache_query_string_mode.value!r}.",
            )
        )
