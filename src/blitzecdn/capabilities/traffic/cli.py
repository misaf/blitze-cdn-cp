"""Inspect saved hourly visitor traffic."""

from collections.abc import Sequence
from typing import Annotated

import typer
from pydantic import BaseModel

from blitzecdn.capabilities.traffic.domain import UsageQuery
from blitzecdn.capabilities.traffic.domain.analytics import Granularity, SiteQuery
from blitzecdn.capabilities.traffic.service.collection import TrafficService
from blitzecdn.entrypoints.cli import common

traffic_app = typer.Typer(help="Saved per-hostname bandwidth and request counts.")
analytics_app = typer.Typer(
    help="Per-site requests, bandwidth, cache hits, status classes and top paths."
)

Start = Annotated[
    str, typer.Option("--start", help="Inclusive UTC hour, ISO 8601 with timezone.")
]
End = Annotated[
    str, typer.Option("--end", help="Exclusive UTC hour, ISO 8601 with timezone.")
]
Edge = Annotated[
    str | None, typer.Option("--edge", help="Exact edge name; all when omitted.")
]
Limit = Annotated[int, typer.Option("--limit", min=1, max=500)]
Offset = Annotated[int, typer.Option("--offset", min=0, max=2**63 - 1)]


@traffic_app.command("usage")
def usage(
    start: Annotated[
        str, typer.Option("--start", help="Inclusive UTC hour, ISO 8601 with timezone.")
    ],
    end: Annotated[
        str, typer.Option("--end", help="Exclusive UTC hour, ISO 8601 with timezone.")
    ],
    hostname: Annotated[
        str | None, typer.Option("--hostname", help="Exact served hostname.")
    ] = None,
    edge: Annotated[str | None, typer.Option("--edge", help="Exact edge name.")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=500)] = 100,
    offset: Annotated[int, typer.Option("--offset", min=0, max=2**63 - 1)] = 0,
    json_output: common.JsonOutput = False,
) -> None:
    """Hourly counters, ordered by hour, hostname and edge; empty hours omitted."""
    query = UsageQuery.model_validate(
        {
            "start": start,
            "end": end,
            "hostname": hostname,
            "edge": edge,
            "limit": limit,
            "offset": offset,
        }
    )
    result = common.service(TrafficService).usage(query)
    common.emit(
        {
            "items": [
                item.model_dump(mode="json", exclude={"paths"}) for item in result.items
            ],
            "total": result.total,
            "limit": query.limit,
            "offset": query.offset,
        },
        json_output=json_output,
    )


@traffic_app.command("status")
def collection_status(
    edge: Annotated[
        str,
        typer.Argument(
            help="Exact edge name, including retired edges with collection history."
        ),
    ],
    json_output: common.JsonOutput = False,
) -> None:
    """Last observed collection state, attempt times, backlog and safe error."""
    common.emit(common.service(TrafficService).status(edge), json_output=json_output)


@analytics_app.command("site")
def site(
    site: Annotated[str, typer.Argument(help="Served hostname.")],
    start: Start,
    end: End,
    edge: Edge = None,
    granularity: Annotated[
        Granularity,
        typer.Option(
            "--granularity", help="hour, or day with start and end at UTC midnight."
        ),
    ] = Granularity.HOUR,
    limit: Limit = 100,
    offset: Offset = 0,
    json_output: common.JsonOutput = False,
) -> None:
    """Totals per UTC hour or day across edges, oldest first; empty ones omitted."""
    query = SiteQuery.model_validate(
        {
            "site": site,
            "start": start,
            "end": end,
            "edge": edge,
            "granularity": granularity,
            "limit": limit,
            "offset": offset,
        }
    )
    result = common.service(TrafficService).site_buckets(query)
    _page(result.items, result.total, query, json_output=json_output)


@analytics_app.command("paths")
def paths(
    site: Annotated[str, typer.Argument(help="Served hostname.")],
    start: Start,
    end: End,
    edge: Edge = None,
    limit: Limit = 100,
    offset: Offset = 0,
    json_output: common.JsonOutput = False,
) -> None:
    """Busiest paths over the range, most requested first; lower-bound counts."""
    query = SiteQuery.model_validate(
        {
            "site": site,
            "start": start,
            "end": end,
            "edge": edge,
            "limit": limit,
            "offset": offset,
        }
    )
    result = common.service(TrafficService).site_paths(query)
    _page(result.items, result.total, query, json_output=json_output)


def _page(
    items: Sequence[BaseModel], total: int, query: SiteQuery, *, json_output: bool
) -> None:
    common.emit(
        {
            "items": [item.model_dump(mode="json") for item in items],
            "total": total,
            "limit": query.limit,
            "offset": query.offset,
        },
        json_output=json_output,
    )
