from fastapi import APIRouter, Depends, status

from blitzecdn.capabilities.origins.api.models import OriginCheckRequest
from blitzecdn.capabilities.origins.service.operations import (
    CHECK_OPERATION,
    check_payload,
)
from blitzecdn.entrypoints.api.dependencies import (
    OperatorDependency,
    ServicesDependency,
    require_operator,
)
from blitzecdn.entrypoints.api.models import Operation, as_queued

router = APIRouter(dependencies=[Depends(require_operator)])


@router.post(
    "/v1/origins/check",
    response_model=Operation,
    status_code=status.HTTP_202_ACCEPTED,
)
def check_origins(
    request: OriginCheckRequest,
    operator: OperatorDependency,
    control: ServicesDependency,
) -> Operation:
    """Queue the edges connecting to their origins; the result is an OriginReport."""
    return as_queued(
        control.queued_operations.submit(
            CHECK_OPERATION, check_payload(operator, host_limit=request.host_limit)
        )
    )
