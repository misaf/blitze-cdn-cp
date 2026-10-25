"""Reading the journal.

Two routes, owned by the journal rather than by any one caller of it: a
deployment and a certificate issuance both keep a workflow and read it back
through the same endpoint.
"""

from fastapi import APIRouter, Depends, Query

from blitzecdn.api.dependencies import ServicesDependency, require_operator
from blitzecdn.api.models import as_operation
from blitzecdn.capabilities.workflows.api.models import Workflow

router = APIRouter(dependencies=[Depends(require_operator)])


@router.get("/v1/workflows", response_model=list[Workflow])
def list_workflows(
    control: ServicesDependency,
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[Workflow]:
    """Durable progress for operations that crossed external systems."""
    return [
        as_operation(item, Workflow)
        for item in control.workflow_history.list_workflows(limit)
    ]


@router.get("/v1/workflows/{workflow_id}", response_model=Workflow)
def get_workflow(
    workflow_id: str,
    control: ServicesDependency,
) -> Workflow:
    return as_operation(control.workflow_history.get(workflow_id), Workflow)
