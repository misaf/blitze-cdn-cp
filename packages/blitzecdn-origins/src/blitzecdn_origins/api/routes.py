from fastapi import APIRouter, Depends

from blitzecdn.api.dependencies import (
    ControlPlaneDependency,
    OperatorDependency,
    require_operator,
)
from blitzecdn.api.models import as_operation
from blitzecdn_origins.api.models import OriginCheckRequest, OriginReport
from blitzecdn_origins.composition import build_origin_check_service

router = APIRouter(dependencies=[Depends(require_operator)])


@router.post("/v1/origins/check", response_model=OriginReport)
def check_origins(
    request: OriginCheckRequest,
    operator: OperatorDependency,
    control: ControlPlaneDependency,
) -> OriginReport:
    """Ask the edges to connect to the origins they proxy to."""
    return as_operation(
        build_origin_check_service(control).check_origins(
            operator, host_limit=request.host_limit
        ),
        OriginReport,
    )
