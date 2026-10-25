from fastapi import APIRouter, Depends, status

from blitzecdn.capabilities.cache.api.models import PurgeRequest, StatsRequest
from blitzecdn.capabilities.cache.service.operations import (
    PURGE_OPERATION,
    STATS_OPERATION,
    purge_payload,
    stats_payload,
)
from blitzecdn.entrypoints.api.dependencies import (
    OperatorDependency,
    ServicesDependency,
    require_operator,
)
from blitzecdn.entrypoints.api.models import Operation, as_queued

router = APIRouter(dependencies=[Depends(require_operator)])


@router.post(
    "/v1/cache/purge",
    response_model=Operation,
    status_code=status.HTTP_202_ACCEPTED,
)
def purge_cache(
    request: PurgeRequest,
    operator: OperatorDependency,
    control: ServicesDependency,
) -> Operation:
    """Queue a purge on the edges; the operation's result is a PurgeResult.

    A request that could only purge nothing is refused here, before queueing.
    The result's ``complete`` and ``failed_hosts`` name edges that may still
    serve the cached copy.
    """
    entries = control.cache.check_purge(
        entries=[entry.to_domain() for entry in request.entries],
        purge_all=request.purge_all,
    )
    return as_queued(
        control.queued_operations.submit(
            PURGE_OPERATION,
            purge_payload(
                operator,
                entries=entries,
                purge_all=request.purge_all,
                host_limit=request.host_limit,
            ),
        )
    )


@router.post(
    "/v1/cache/stats",
    response_model=Operation,
    status_code=status.HTTP_202_ACCEPTED,
)
def cache_stats(
    request: StatsRequest,
    operator: OperatorDependency,
    control: ServicesDependency,
) -> Operation:
    """Queue a collection of cache effectiveness; the result is a CacheStatsReport."""
    return as_queued(
        control.queued_operations.submit(
            STATS_OPERATION, stats_payload(operator, host_limit=request.host_limit)
        )
    )
