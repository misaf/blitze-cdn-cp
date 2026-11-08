"""Per-site analytics read from stored traffic; no edge is contacted."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from dishka import FromDishka
from dishka.integrations.fastapi import DishkaSyncRoute
from fastapi import APIRouter, Depends, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError

from blitzecdn.capabilities.analytics.api.models import AnalyticsBucket, PathCounters
from blitzecdn.capabilities.analytics.domain import Granularity, PathsQuery, SiteQuery
from blitzecdn.capabilities.analytics.service import AnalyticsService
from blitzecdn.entrypoints.api.dependencies import require_operator
from blitzecdn.entrypoints.api.pagination import Page, PaginationParams, stored_page

router = APIRouter(
    route_class=DishkaSyncRoute, dependencies=[Depends(require_operator)]
)

Start = Annotated[datetime, Query(description="Inclusive UTC hour; timezone required.")]
End = Annotated[datetime, Query(description="Exclusive UTC hour; timezone required.")]


def _query[Q: BaseModel](model: type[Q], **fields: object) -> Q:
    """Domain validation reported as query-parameter errors, like FastAPI's own."""
    try:
        return model.model_validate(fields)
    except ValidationError as exc:
        raise RequestValidationError(
            [
                {**error, "loc": ("query", *error["loc"])}
                for error in exc.errors(
                    include_url=False, include_context=False, include_input=False
                )
            ]
        ) from exc


@router.get("/v3/analytics/sites/{site}", response_model=Page[AnalyticsBucket])
def site_series(
    site: str,
    analytics: FromDishka[AnalyticsService],
    params: PaginationParams,
    start: Start,
    end: End,
    granularity: Annotated[
        Granularity, Query(description="Bucket size; day needs UTC-midnight bounds.")
    ] = Granularity.HOUR,
) -> Page[AnalyticsBucket]:
    """Requests, bytes, cache hit ratio and status classes per hour or day.

    The series is dense: ``total`` counts every bucket in the range, and empty
    buckets are zero. Hostnames are attributed to the site with its current
    server names, wildcards resolved as nginx would. Counters may lag while
    collection is pending; see ``/v3/traffic/status/{edge}``.
    """
    query = _query(
        SiteQuery,
        site=site,
        start=start,
        end=end,
        granularity=granularity,
        limit=params.limit,
        offset=params.offset,
    )
    return stored_page(
        analytics.series(query),
        params,
        lambda item: AnalyticsBucket.model_validate(item.model_dump()),
    )


@router.get("/v3/analytics/sites/{site}/paths", response_model=Page[PathCounters])
def site_paths(
    site: str,
    analytics: FromDishka[AnalyticsService],
    params: PaginationParams,
    start: Start,
    end: End,
) -> Page[PathCounters]:
    """The site's most requested paths over the range, most requested first.

    Ranked at collection time: each edge keeps a bounded top list per hostname
    and hour, so counts are lower bounds and rarely requested paths are absent.
    """
    query = _query(
        PathsQuery,
        site=site,
        start=start,
        end=end,
        limit=params.limit,
        offset=params.offset,
    )
    return stored_page(
        analytics.top_paths(query),
        params,
        lambda item: PathCounters.model_validate(item.model_dump()),
    )
