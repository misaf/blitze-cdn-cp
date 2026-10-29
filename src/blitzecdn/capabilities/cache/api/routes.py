from dishka import FromDishka
from dishka.integrations.fastapi import DishkaSyncRoute
from fastapi import APIRouter, Depends, Response, status

from blitzecdn.capabilities.cache.api.models import PurgeRequest, StatsRequest
from blitzecdn.capabilities.cache.service import CacheService
from blitzecdn.capabilities.cache.service.operations import (
    PURGE_OPERATION,
    STATS_OPERATION,
    purge_payload,
    stats_payload,
)
from blitzecdn.capabilities.jobs import OperationService
from blitzecdn.entrypoints.api.dependencies import (
    OperatorDependency,
    require_operator,
)
from blitzecdn.entrypoints.api.models import Operation, as_queued
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
    entries = cache.check_purge(
        entries=[entry.to_domain() for entry in request.entries],
        purge_all=request.purge_all,
    )
    queued = as_queued(
        queued_operations.submit(
            PURGE_OPERATION,
            purge_payload(
                operator,
                entries=entries,
                purge_all=request.purge_all,
                host_limit=request.host_limit,
            ),
        )
    )
    polling_headers(response, active=True, location=f"/v3/operations/{queued.id}")
    return queued


@router.post(
    "/v3/cache/stats",
    response_model=Operation,
    status_code=status.HTTP_202_ACCEPTED,
    responses={202: ACCEPTED_RESPONSE},
)
def cache_stats(
    response: Response,
    request: StatsRequest,
    operator: OperatorDependency,
    queued_operations: FromDishka[OperationService],
) -> Operation:
    """Queue a collection of cache effectiveness; the result is a CacheStatsReport."""
    queued = as_queued(
        queued_operations.submit(
            STATS_OPERATION, stats_payload(operator, host_limit=request.host_limit)
        )
    )
    polling_headers(response, active=True, location=f"/v3/operations/{queued.id}")
    return queued
