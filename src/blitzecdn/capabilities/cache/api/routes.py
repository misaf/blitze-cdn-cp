from datetime import datetime
from typing import Annotated

from dishka import FromDishka
from dishka.integrations.fastapi import DishkaSyncRoute
from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from blitzecdn.capabilities.cache.api.models import (
    CacheHour,
    ConnectionsRequest,
    PurgeRequest,
)
from blitzecdn.capabilities.cache.service import CacheService
from blitzecdn.capabilities.cache.service.operations import (
    CacheConnectionsJob,
    CachePurgeJob,
)
from blitzecdn.capabilities.jobs import OperationService
from blitzecdn.capabilities.traffic import UsageQuery
from blitzecdn.entrypoints.api.dependencies import (
    OperatorDependency,
    require_operator,
)
from blitzecdn.entrypoints.api.models import Operation, as_queued
from blitzecdn.entrypoints.api.pagination import Page, PaginationParams, stored_page
from blitzecdn.entrypoints.api.responses import (
    ACCEPTED_RESPONSE,
    polling_headers,
)

router = APIRouter(
    route_class=DishkaSyncRoute, dependencies=[Depends(require_operator)]
)


@router.post(
    "/v3/cache/purge",
    response_model=Operation,
    status_code=status.HTTP_202_ACCEPTED,
    responses={202: ACCEPTED_RESPONSE},
)
def purge_cache(
    response: Response,
    request: PurgeRequest,
    operator: OperatorDependency,
    cache: FromDishka[CacheService],
    queued_operations: FromDishka[OperationService],
) -> Operation:
    """Queue a purge on the edges; the operation's result is a PurgeResult.

    A request that could only purge nothing is refused here, before queueing.
    The result's ``complete`` and ``failed_hosts`` name edges that may still
    serve the cached copy.
    """
    targets = cache.check_purge(
        entries=[entry.to_domain() for entry in request.entries],
        prefixes=[prefix.to_domain() for prefix in request.prefixes],
        tags=request.tags,
        purge_all=request.purge_all,
    )
    queued = as_queued(
        queued_operations.submit(
            CachePurgeJob(
                operator=operator,
                entries=tuple(targets.entries),
                prefixes=tuple(targets.prefixes),
                tags=tuple(targets.tags),
                purge_all=request.purge_all,
                host_limit=request.host_limit,
            )
        )
    )
    polling_headers(response, active=True, location=f"/v3/operations/{queued.id}")
    return queued


@router.get("/v3/cache/stats", response_model=Page[CacheHour])
def cache_stats(
    cache: FromDishka[CacheService],
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
) -> Page[CacheHour]:
    """Stored hourly cache outcomes, ordered by hour, hostname and edge.

    Counted by traffic collection from every log record exactly once; no edge
    is contacted. Empty hours are omitted, and counters may lag while
    collection is pending. The hit ratio counts only requests that consulted
    the cache; redirects, errors nginx served itself and sites with caching
    disabled are NONE, not misses.
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
        cache.stats(query),
        params,
        lambda item: CacheHour.model_validate(item.model_dump()),
    )


@router.post(
    "/v3/cache/connections",
    response_model=Operation,
    status_code=status.HTTP_202_ACCEPTED,
    responses={202: ACCEPTED_RESPONSE},
)
def cache_connections(
    response: Response,
    request: ConnectionsRequest,
    operator: OperatorDependency,
    queued_operations: FromDishka[OperationService],
) -> Operation:
    """Queue a live read of nginx's connection counters; the result is a report."""
    queued = as_queued(
        queued_operations.submit(
            CacheConnectionsJob(operator=operator, host_limit=request.host_limit)
        )
    )
    polling_headers(response, active=True, location=f"/v3/operations/{queued.id}")
    return queued
