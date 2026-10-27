from dishka import FromDishka
from dishka.integrations.fastapi import DishkaSyncRoute
from fastapi import APIRouter, Depends, status

from blitzecdn.capabilities.jobs import OperationService
from blitzecdn.capabilities.origins.api.models import OriginCheckRequest
from blitzecdn.capabilities.origins.service.operations import (
    CHECK_OPERATION,
    check_payload,
)
from blitzecdn.entrypoints.api.dependencies import (
    OperatorDependency,
    require_operator,
)
from blitzecdn.entrypoints.api.models import Operation, as_queued

router = APIRouter(
    route_class=DishkaSyncRoute, dependencies=[Depends(require_operator)]
)


@router.post(
    "/v3/origins/check",
    response_model=Operation,
    status_code=status.HTTP_202_ACCEPTED,
)
def check_origins(
    request: OriginCheckRequest,
    operator: OperatorDependency,
    queued_operations: FromDishka[OperationService],
) -> Operation:
    """Queue the edges connecting to their origins; the result is an OriginReport."""
    return as_queued(
        queued_operations.submit(
            CHECK_OPERATION, check_payload(operator, host_limit=request.host_limit)
        )
    )
