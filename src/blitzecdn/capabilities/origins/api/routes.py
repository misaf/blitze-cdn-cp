from dishka import FromDishka
from dishka.integrations.fastapi import DishkaSyncRoute
from fastapi import APIRouter, Depends, Response, status

from blitzecdn.capabilities.jobs import OperationService
from blitzecdn.capabilities.origins.api.models import OriginCheckRequest
from blitzecdn.capabilities.origins.service.operations import OriginCheckJob
from blitzecdn.core.ports.idempotency import IdempotencyKeyStore
from blitzecdn.delivery.api.dependencies import (
    OperatorDependency,
    require_operator,
)
from blitzecdn.delivery.api.errors import (
    BODY_RESPONSES,
    CONFLICT_RESPONSES,
    OPERATOR_RESPONSES,
)
from blitzecdn.delivery.api.idempotency import (
    IdempotencyKeyDependency,
    idempotent,
)
from blitzecdn.delivery.api.models import Operation, as_queued
from blitzecdn.delivery.api.responses import (
    ACCEPTED_RESPONSE,
    polling_headers,
)

router = APIRouter(
    route_class=DishkaSyncRoute,
    dependencies=[Depends(require_operator)],
    responses=OPERATOR_RESPONSES,
)


@router.post(
    "/v3/origins/check",
    response_model=Operation,
    status_code=status.HTTP_202_ACCEPTED,
    responses={202: ACCEPTED_RESPONSE, **CONFLICT_RESPONSES, **BODY_RESPONSES},
)
def check_origins(
    response: Response,
    request: OriginCheckRequest,
    operator: OperatorDependency,
    queued_operations: FromDishka[OperationService],
    idempotency: IdempotencyKeyDependency,
    keys: FromDishka[IdempotencyKeyStore],
) -> Operation:
    """Queue the edges connecting to their origins; the result is an OriginReport."""
    queued = as_queued(
        idempotent(
            idempotency,
            keys,
            create=lambda: queued_operations.submit(
                OriginCheckJob(operator=operator, host_limit=request.host_limit)
            ),
            replay=queued_operations.get,
        )
    )
    polling_headers(response, active=True, location=f"/v3/operations/{queued.id}")
    return queued
