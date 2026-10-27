"""Reading the journal, for every kind of workflow."""

from dishka import FromDishka
from dishka.integrations.fastapi import DishkaSyncRoute
from fastapi import APIRouter, Depends, Query

from blitzecdn.capabilities.workflows.api.models import Workflow
from blitzecdn.capabilities.workflows.ports import WorkflowHistory
from blitzecdn.entrypoints.api.dependencies import require_operator
from blitzecdn.entrypoints.api.models import as_operation

router = APIRouter(
    route_class=DishkaSyncRoute, dependencies=[Depends(require_operator)]
)


@router.get("/v3/workflows", response_model=list[Workflow])
def list_workflows(
    workflow_history: FromDishka[WorkflowHistory],
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[Workflow]:
    """Durable progress for operations that crossed external systems."""
    return [
        as_operation(item, Workflow) for item in workflow_history.list_workflows(limit)
    ]


@router.get("/v3/workflows/{workflow_id}", response_model=Workflow)
def get_workflow(
    workflow_id: str,
    workflow_history: FromDishka[WorkflowHistory],
) -> Workflow:
    return as_operation(workflow_history.get(workflow_id), Workflow)
