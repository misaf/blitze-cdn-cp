from dishka import FromDishka
from dishka.integrations.fastapi import DishkaSyncRoute
from fastapi import APIRouter, Depends

from blitzecdn.capabilities.jobs.service import OperationService
from blitzecdn.entrypoints.api.dependencies import require_operator
from blitzecdn.entrypoints.api.models import Operation, as_queued

router = APIRouter(
    route_class=DishkaSyncRoute, dependencies=[Depends(require_operator)]
)


@router.get("/v3/operations/{operation_id}", response_model=Operation)
def operation(
    operation_id: str, queued_operations: FromDishka[OperationService]
) -> Operation:
    """How a queued edge operation stands, and its result once it has one."""
    return as_queued(queued_operations.get(operation_id))
