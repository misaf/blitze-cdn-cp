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
