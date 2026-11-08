"""Inspect per-site analytics from stored traffic."""

from __future__ import annotations

from typing import Annotated, Any

import typer

from blitzecdn.capabilities.analytics.domain import Granularity, PathsQuery, SiteQuery
from blitzecdn.capabilities.analytics.service import AnalyticsService
from blitzecdn.core.domain.pagination import PageResult
from blitzecdn.entrypoints.cli import common

analytics_app = typer.Typer(help="Per-site requests, bandwidth, cache and status.")

Site = Annotated[str, typer.Argument(help="The CDN site (host) name.")]
Start = Annotated[
    str, typer.Option("--start", help="Inclusive UTC hour, ISO 8601 with timezone.")
]
End = Annotated[
    str, typer.Option("--end", help="Exclusive UTC hour, ISO 8601 with timezone.")
]
Limit = Annotated[int, typer.Option("--limit", min=1, max=500)]
Offset = Annotated[int, typer.Option("--offset", min=0, max=2**63 - 1)]


def _emit(
    result: PageResult[Any], limit: int, offset: int, *, json_output: bool
) -> None:
    common.emit(
        {
            "items": [item.model_dump(mode="json") for item in result.items],
            "total": result.total,
            "limit": limit,
            "offset": offset,
        },
        json_output=json_output,
    )


@analytics_app.command("site")
def site_series(
    site: Site,
    start: Start,
    end: End,
    granularity: Annotated[
        Granularity, typer.Option("--granularity", help="Bucket size.")
    ] = Granularity.HOUR,
    limit: Limit = 100,
    offset: Offset = 0,
    json_output: common.JsonOutput = False,
) -> None:
    """Requests, bytes, hit ratio and status classes per hour or UTC day."""
    query = SiteQuery.model_validate(
        {
            "site": site,
            "start": start,
            "end": end,
            "granularity": granularity,
            "limit": limit,
            "offset": offset,
        }
    )
    _emit(
        common.service(AnalyticsService).series(query),
        query.limit,
        query.offset,
        json_output=json_output,
    )


@analytics_app.command("paths")
def site_paths(
    site: Site,
    start: Start,
    end: End,
    limit: Limit = 100,
    offset: Offset = 0,
    json_output: common.JsonOutput = False,
) -> None:
    """The site's most requested paths over the range; counts are lower bounds."""
    query = PathsQuery.model_validate(
        {"site": site, "start": start, "end": end, "limit": limit, "offset": offset}
    )
    _emit(
        common.service(AnalyticsService).top_paths(query),
        query.limit,
        query.offset,
        json_output=json_output,
    )
