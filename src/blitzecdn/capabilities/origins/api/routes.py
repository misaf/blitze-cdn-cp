from dishka import FromDishka
from dishka.integrations.fastapi import DishkaSyncRoute
from fastapi import APIRouter, Depends, Response, status

from blitzecdn.capabilities.jobs import OperationService
from blitzecdn.capabilities.origins.api.models import OriginCheckRequest
from blitzecdn.capabilities.origins.service.operations import OriginCheckJob
from blitzecdn.delivery.api.dependencies import (
    OperatorDependency,
    require_operator,
)
from blitzecdn.delivery.api.models import Operation, as_queued
from blitzecdn.delivery.api.responses import (
    ACCEPTED_RESPONSE,
    polling_headers,
)

router = APIRouter(
    route_class=DishkaSyncRoute, dependencies=[Depends(require_operator)]
)


@router.post(
    "/v3/origins/check",
    response_model=Operation,
    status_code=status.HTTP_202_ACCEPTED,
    responses={202: ACCEPTED_RESPONSE},
)
def check_origins(
    response: Response,
    request: OriginCheckRequest,
    operator: OperatorDependency,
    queued_operations: FromDishka[OperationService],
) -> Operation:
    """Queue the edges connecting to their origins; the result is an OriginReport."""
    queued = as_queued(
        queued_operations.submit(
            OriginCheckJob(operator=operator, host_limit=request.host_limit)
        )
    )
    polling_headers(response, active=True, location=f"/v3/operations/{queued.id}")
    return queued
