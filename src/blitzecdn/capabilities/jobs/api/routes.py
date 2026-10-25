from fastapi import APIRouter, Depends

from blitzecdn.entrypoints.api.dependencies import ServicesDependency, require_operator
from blitzecdn.entrypoints.api.models import Operation, as_queued

router = APIRouter(dependencies=[Depends(require_operator)])


@router.get("/v1/operations/{operation_id}", response_model=Operation)
def operation(operation_id: str, control: ServicesDependency) -> Operation:
    """How a queued edge operation stands, and its result once it has one."""
    return as_queued(control.queued_operations.get(operation_id))
