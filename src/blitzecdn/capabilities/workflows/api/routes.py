"""Reading the journal.

Two routes, and they were `deployments`' — `/v1/workflows` sat in
`deployments/api/routes.py` because that is where the first caller of the
journal was, not because a deployment owns one. A certificate issuance keeps a
workflow too, and reads it back through the same endpoint.
"""

from fastapi import APIRouter, Depends, Query

from blitzecdn.api.dependencies import ControlPlaneDependency, require_operator
from blitzecdn.api.models import as_operation
from blitzecdn.capabilities.workflows.api.models import Workflow

router = APIRouter(dependencies=[Depends(require_operator)])


@router.get("/v1/workflows", response_model=list[Workflow])
def list_workflows(
    control: ControlPlaneDependency,
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
    control: ControlPlaneDependency,
) -> Workflow:
    return as_operation(control.workflow_history.get(workflow_id), Workflow)
