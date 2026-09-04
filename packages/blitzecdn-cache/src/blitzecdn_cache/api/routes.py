from fastapi import APIRouter, Depends, Response, status

from blitzecdn.api.dependencies import (
    ControlPlaneDependency,
    OperatorDependency,
    require_operator,
)
from blitzecdn.api.models import as_operation
from blitzecdn_cache.api.models import (
    CacheStatsReport,
    PurgeRequest,
    PurgeResult,
    StatsRequest,
)
from blitzecdn_cache.composition import build_cache_service

router = APIRouter(dependencies=[Depends(require_operator)])


@router.post(
    "/v1/cache/purge",
    response_model=PurgeResult,
    responses={
        status.HTTP_409_CONFLICT: {
            "model": PurgeResult,
            "description": (
                "Some edge did not purge. The body is the same PurgeResult "
                "as a success, with complete=false and failed_hosts naming "
                "the edges that may still be serving the cached copy."
            ),
        }
    },
)
def purge_cache(
    request: PurgeRequest,
    operator: OperatorDependency,
    control: ControlPlaneDependency,
    response: Response,
) -> PurgeResult:
    """Remove cached responses from the edges and report partial failure."""
    result = build_cache_service(control).purge_cache(
        operator,
        entries=[entry.to_domain() for entry in request.entries],
        purge_all=request.purge_all,
        host_limit=request.host_limit,
    )
    if not result.complete:
        response.status_code = status.HTTP_409_CONFLICT
    return as_operation(result, PurgeResult)


@router.post("/v1/cache/stats", response_model=CacheStatsReport)
def cache_stats(
    request: StatsRequest,
    operator: OperatorDependency,
    control: ControlPlaneDependency,
) -> CacheStatsReport:
    """Collect cache effectiveness from the edges."""
    return as_operation(
        build_cache_service(control).cache_stats(
            operator, host_limit=request.host_limit
        ),
        CacheStatsReport,
    )
