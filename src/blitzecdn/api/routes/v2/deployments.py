from fastapi import APIRouter, Depends, Query, status

from blitzecdn.api.dependencies import (
    ControlPlaneDependency,
    OperatorDependency,
    require_operator,
)
from blitzecdn.api.v2_operations import Deployment, DriftReport, Workflow, as_v2
from blitzecdn.api.v2_requests import DeployRequest, DriftRequest, RollbackRequest

router = APIRouter(dependencies=[Depends(require_operator)])


@router.post(
    "/v2/deployments",
    response_model=Deployment,
    status_code=status.HTTP_202_ACCEPTED,
)
def deploy(
    request: DeployRequest,
    operator: OperatorDependency,
    control: ControlPlaneDependency,
) -> Deployment:
    """Queue a convergence; poll GET /v2/deployments/{id} for the outcome."""
    return as_v2(
        control.deployments.submit_deployment(
            operator, check=request.check, host_limit=request.host_limit
        ),
        Deployment,
    )


@router.get("/v2/deployments", response_model=list[Deployment])
def deployments(
    control: ControlPlaneDependency,
    limit: int = Query(20, ge=1, le=100),
) -> list[Deployment]:
    return [
        as_v2(item, Deployment) for item in control.deployments.list_deployments(limit)
    ]


@router.get("/v2/workflows", response_model=list[Workflow])
def list_workflows(
    control: ControlPlaneDependency,
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[Workflow]:
    """Durable progress for operations that crossed external systems."""
    return [
        as_v2(item, Workflow) for item in control.workflow_history.list_workflows(limit)
    ]


@router.get("/v2/workflows/{workflow_id}", response_model=Workflow)
def get_workflow(
    workflow_id: str,
    control: ControlPlaneDependency,
) -> Workflow:
    return as_v2(control.workflow_history.get(workflow_id), Workflow)


@router.get("/v2/deployments/{deployment_id}", response_model=Deployment)
def deployment(
    deployment_id: str,
    control: ControlPlaneDependency,
) -> Deployment:
    return as_v2(control.deployments.get_deployment(deployment_id), Deployment)


@router.post(
    "/v2/drift", response_model=Deployment, status_code=status.HTTP_202_ACCEPTED
)
def check_drift(
    request: DriftRequest,
    operator: OperatorDependency,
    control: ControlPlaneDependency,
) -> Deployment:
    return as_v2(
        control.deployments.submit_deployment(
            operator, check=True, host_limit=request.host_limit
        ),
        Deployment,
    )


@router.get("/v2/deployments/{deployment_id}/drift", response_model=DriftReport)
def drift_report(
    deployment_id: str,
    control: ControlPlaneDependency,
) -> DriftReport:
    return as_v2(control.deployments.drift_report(deployment_id), DriftReport)


@router.post(
    "/v2/rollbacks",
    response_model=Deployment,
    status_code=status.HTTP_202_ACCEPTED,
)
def rollback(
    request: RollbackRequest,
    operator: OperatorDependency,
    control: ControlPlaneDependency,
) -> Deployment:
    return as_v2(
        control.deployments.submit_rollback(
            operator, request.deployment_id, check=request.check
        ),
        Deployment,
    )
