"""Read persisted traffic without contacting edges."""

from datetime import datetime
from typing import Annotated

from dishka import FromDishka
from dishka.integrations.fastapi import DishkaSyncRoute
from fastapi import APIRouter, Depends, Query
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from blitzecdn.capabilities.traffic.api.models import TrafficUsage
from blitzecdn.capabilities.traffic.domain import UsageQuery
from blitzecdn.capabilities.traffic.service.collection import TrafficService
from blitzecdn.entrypoints.api.dependencies import require_operator
from blitzecdn.entrypoints.api.pagination import Page, PaginationParams, stored_page

router = APIRouter(
    route_class=DishkaSyncRoute, dependencies=[Depends(require_operator)]
)


@router.get("/v3/traffic/usage", response_model=Page[TrafficUsage])
def usage(
    traffic: FromDishka[TrafficService],
    params: PaginationParams,
    start: Annotated[
        datetime, Query(description="Inclusive UTC hour; timezone required.")
    ],
    end: Annotated[
        datetime, Query(description="Exclusive UTC hour; timezone required.")
    ],
    hostname: Annotated[str | None, Query(description="Exact served hostname.")] = None,
    edge: Annotated[
        str | None, Query(description="Exact edge name, including retired edges.")
    ] = None,
) -> Page[TrafficUsage]:
    """Saved hourly counters, ordered by hour, hostname and edge.

    Empty hours are omitted. Counters may lag while collection is pending.
    bytes_sent includes HTTP headers; body_bytes_sent excludes them.
    """
    try:
        query = UsageQuery(
            start=start,
            end=end,
            hostname=hostname,
            edge=edge,
            limit=params.limit,
            offset=params.offset,
        )
    except ValidationError as exc:
        raise RequestValidationError(
            [
                {**error, "loc": ("query", *error["loc"])}
                for error in exc.errors(
                    include_url=False, include_context=False, include_input=False
                )
            ]
        ) from exc
    return stored_page(
        traffic.usage(query),
        params,
        lambda item: TrafficUsage.model_validate(item.model_dump()),
    )
