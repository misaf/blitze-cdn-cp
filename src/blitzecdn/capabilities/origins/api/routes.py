from fastapi import APIRouter, Depends

from blitzecdn.capabilities.origins.api.models import (
    OriginCheckRequest,
    OriginReport,
)
from blitzecdn.entrypoints.api.dependencies import (
    OperatorDependency,
    ServicesDependency,
    require_operator,
)
from blitzecdn.entrypoints.api.models import as_operation

router = APIRouter(dependencies=[Depends(require_operator)])


@router.post("/v1/origins/check", response_model=OriginReport)
def check_origins(
    request: OriginCheckRequest,
    operator: OperatorDependency,
    control: ServicesDependency,
) -> OriginReport:
    """Ask the edges to connect to the origins they proxy to."""
    return as_operation(
        control.origins.check_origins(operator, host_limit=request.host_limit),
        OriginReport,
    )
