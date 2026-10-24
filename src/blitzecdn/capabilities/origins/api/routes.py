from fastapi import APIRouter, Depends

from blitzecdn.api.dependencies import (
    ControlPlaneDependency,
    OperatorDependency,
    require_operator,
)
from blitzecdn.api.models import as_operation
from blitzecdn.capabilities.origins.api.models import (
    OriginCheckRequest,
    OriginReport,
)

router = APIRouter(dependencies=[Depends(require_operator)])


@router.post("/v1/origins/check", response_model=OriginReport)
def check_origins(
    request: OriginCheckRequest,
    operator: OperatorDependency,
    control: ControlPlaneDependency,
) -> OriginReport:
    """Ask the edges to connect to the origins they proxy to."""
    return as_operation(
        control.origins.check_origins(operator, host_limit=request.host_limit),
        OriginReport,
    )
