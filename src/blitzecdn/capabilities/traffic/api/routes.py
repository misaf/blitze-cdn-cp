"""Read persisted traffic without contacting edges."""

from datetime import datetime
from typing import Annotated

from dishka import FromDishka
from dishka.integrations.fastapi import DishkaSyncRoute
from fastapi import APIRouter, Depends, Query, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError

from blitzecdn.capabilities.traffic.api.models import (
    CollectionStatus,
    SiteBucket,
    SitePath,
    TrafficUsage,
)
from blitzecdn.capabilities.traffic.domain import UsageQuery
from blitzecdn.capabilities.traffic.domain.analytics import Granularity, SiteQuery
from blitzecdn.capabilities.traffic.service.collection import TrafficService
from blitzecdn.delivery.api.dependencies import require_operator
from blitzecdn.delivery.api.errors import (
    NOT_FOUND_RESPONSES,
    OPERATOR_RESPONSES,
)
from blitzecdn.delivery.api.pagination import Page, PaginationParams, stored_page

router = APIRouter(
    route_class=DishkaSyncRoute,
    dependencies=[Depends(require_operator)],
    responses=OPERATOR_RESPONSES,
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
    query = _validated(
        UsageQuery,
        start=start,
        end=end,
        hostname=hostname,
        edge=edge,
        limit=params.limit,
        offset=params.offset,
    )
    return stored_page(
        traffic.usage(query),
        params,
        lambda item: TrafficUsage.model_validate(item.model_dump(exclude={"paths"})),
    )


Start = Annotated[datetime, Query(description="Inclusive UTC hour; timezone required.")]
End = Annotated[datetime, Query(description="Exclusive UTC hour; timezone required.")]
Edge = Annotated[
    str | None,
    Query(description="Exact edge name, including retired edges; all when omitted."),
]


@router.get(
    "/v3/traffic/sites/{site}",
    response_model=Page[SiteBucket],
    responses=NOT_FOUND_RESPONSES,
)
def site_analytics(
    site: str,
    traffic: FromDishka[TrafficService],
    params: PaginationParams,
    start: Start,
    end: End,
    edge: Edge = None,
    granularity: Annotated[
        Granularity,
        Query(description="UTC hours, or UTC days with start and end at midnight."),
    ] = Granularity.HOUR,
) -> Page[SiteBucket]:
    """A served hostname's requests, bytes, cache hits and status classes.

    One item per UTC hour or day with traffic, oldest first, summed across
    edges from stored hourly rows; no edge is contacted. Empty buckets are
    omitted, and counters may lag while collection is pending. Hours collected
    before status classes were counted have no status counts.
    """
    query = _validated(
        SiteQuery,
        site=site,
        start=start,
        end=end,
        edge=edge,
        granularity=granularity,
        limit=params.limit,
        offset=params.offset,
    )
    return stored_page(
        traffic.site_buckets(query),
        params,
        lambda item: SiteBucket.model_validate(item.model_dump()),
    )


@router.get(
    "/v3/traffic/sites/{site}/paths",
    response_model=Page[SitePath],
    responses=NOT_FOUND_RESPONSES,
)
def site_paths(
    site: str,
    traffic: FromDishka[TrafficService],
    params: PaginationParams,
    start: Start,
    end: End,
    edge: Edge = None,
) -> Page[SitePath]:
    """A served hostname's busiest paths, most requested first.

    Each edge keeps only its busiest paths per hour, without query strings, so
    counts are lower bounds and a path busy in few hours may be missing.
    """
    query = _validated(
        SiteQuery,
        site=site,
        start=start,
        end=end,
        edge=edge,
        limit=params.limit,
        offset=params.offset,
    )
    return stored_page(
        traffic.site_paths(query),
        params,
        lambda item: SitePath.model_validate(item.model_dump()),
    )


def _validated[Q: BaseModel](model: type[Q], **values: object) -> Q:
    """Domain validation reported as a 422 against the query, like FastAPI's."""
    try:
        return model.model_validate(values)
    except ValidationError as exc:
        raise RequestValidationError(
            [
                {
                    **error,
                    "loc": (
                        "path" if error["loc"][:1] == ("site",) else "query",
                        *error["loc"],
                    ),
                }
                for error in exc.errors(
                    include_url=False, include_context=False, include_input=False
                )
            ]
        ) from exc


@router.get(
    "/v3/traffic/status/{edge}",
    response_model=CollectionStatus,
    responses=NOT_FOUND_RESPONSES,
)
def collection_status(
    edge: str, response: Response, traffic: FromDishka[TrafficService]
) -> CollectionStatus:
    """Last observed collection state; this does not trigger collection.

    Current means recently caught up with available logs, not complete billing
    coverage or proof of zero traffic. Unknown edges without history return 404.
    """
    response.headers["Cache-Control"] = "no-store"
    return CollectionStatus.model_validate(traffic.status(edge).model_dump())
