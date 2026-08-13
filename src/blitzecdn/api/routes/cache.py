from fastapi import APIRouter, Response, status

from blitzecdn.api.dependencies import ControlPlaneDependency, OperatorDependency
from blitzecdn.api_models import PurgeRequest, StatsRequest
from blitzecdn.domain.cache import CacheStatsReport, PurgeResult

router = APIRouter()


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
    result = control.cache.purge_cache(
        operator,
        entries=request.entries,
        purge_all=request.purge_all,
        host_limit=request.host_limit,
    )
    if not result.complete:
        response.status_code = status.HTTP_409_CONFLICT
    return result


@router.post("/v1/cache/stats", response_model=CacheStatsReport)
def cache_stats(
    request: StatsRequest,
    operator: OperatorDependency,
    control: ControlPlaneDependency,
) -> CacheStatsReport:
    """Collect cache effectiveness from the edges."""
    return control.cache.cache_stats(operator, host_limit=request.host_limit)
