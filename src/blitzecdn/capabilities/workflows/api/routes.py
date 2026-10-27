"""Reading the journal, for every kind of workflow."""

from dishka import FromDishka
from dishka.integrations.fastapi import DishkaSyncRoute
from fastapi import APIRouter, Depends

from blitzecdn.capabilities.workflows.api.models import Workflow
from blitzecdn.capabilities.workflows.ports import WorkflowHistory
from blitzecdn.entrypoints.api.dependencies import require_operator
from blitzecdn.entrypoints.api.models import as_operation
from blitzecdn.entrypoints.api.pagination import Page, PaginationParams, stored_page

router = APIRouter(
    route_class=DishkaSyncRoute, dependencies=[Depends(require_operator)]
)


@router.get("/v3/workflows", response_model=Page[Workflow])
def list_workflows(
    workflow_history: FromDishka[WorkflowHistory],
    params: PaginationParams,
) -> Page[Workflow]:
    return stored_page(
        workflow_history.paginate_workflows(limit=params.limit, offset=params.offset),
        params,
        lambda item: as_operation(item, Workflow),
    )


@router.get("/v3/workflows/{workflow_id}", response_model=Workflow)
def get_workflow(
    workflow_id: str,
    workflow_history: FromDishka[WorkflowHistory],
) -> Workflow:
    return as_operation(workflow_history.get(workflow_id), Workflow)
