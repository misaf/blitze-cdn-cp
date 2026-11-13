from dishka import FromDishka
from dishka.integrations.fastapi import DishkaSyncRoute
from fastapi import APIRouter, Depends, Response

from blitzecdn.capabilities.jobs.service import OperationService
from blitzecdn.delivery.api.dependencies import require_operator
from blitzecdn.delivery.api.errors import (
    NOT_FOUND_RESPONSES,
    OPERATOR_RESPONSES,
)
from blitzecdn.delivery.api.models import Operation, as_queued
from blitzecdn.delivery.api.responses import POLL_RESPONSE, polling_headers

router = APIRouter(
    tags=["operations"],
    route_class=DishkaSyncRoute,
    dependencies=[Depends(require_operator)],
    responses=OPERATOR_RESPONSES,
)


@router.get(
    "/v3/operations/{operation_id}",
    response_model=Operation,
    responses={200: POLL_RESPONSE, **NOT_FOUND_RESPONSES},
    operation_id="operations_get",
)
def operation(
    response: Response,
    operation_id: str,
    queued_operations: FromDishka[OperationService],
) -> Operation:
    """How a queued edge operation stands, and its result once it has one."""
    current = as_queued(queued_operations.get(operation_id))
    polling_headers(response, active=not current.done)
    return current
