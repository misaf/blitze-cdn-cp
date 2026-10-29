"""Inspect saved hourly visitor traffic."""

from typing import Annotated

import typer

from blitzecdn.capabilities.traffic.domain import UsageQuery
from blitzecdn.capabilities.traffic.service.collection import TrafficService
from blitzecdn.entrypoints.cli import common

traffic_app = typer.Typer(help="Saved per-hostname bandwidth and request counts.")


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
            "items": [item.model_dump(mode="json") for item in result.items],
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
