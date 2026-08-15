from fastapi import APIRouter, Depends, Query, status

from blitzecdn.api.dependencies import (
    ControlPlaneDependency,
    OperatorDependency,
    require_operator,
)
from blitzecdn.api_models import DeployRequest, DriftRequest, RollbackRequest
from blitzecdn.domain.deployments import Deployment, DriftReport
from blitzecdn.domain.operations import Workflow

router = APIRouter(dependencies=[Depends(require_operator)])


@router.post(
    "/v1/deployments",
    response_model=Deployment,
    status_code=status.HTTP_202_ACCEPTED,
)
def deploy(
    request: DeployRequest,
    operator: OperatorDependency,
    control: ControlPlaneDependency,
) -> Deployment:
    """Queue a convergence; poll GET /v1/deployments/{id} for the outcome."""
    return control.deployments.submit_deployment(
        operator, check=request.check, host_limit=request.host_limit
    )


@router.get("/v1/deployments", response_model=list[Deployment])
def deployments(
    control: ControlPlaneDependency,
    limit: int = Query(20, ge=1, le=100),
) -> list[Deployment]:
    return control.deployments.list_deployments(limit)


@router.get("/v1/workflows", response_model=list[Workflow])
def list_workflows(
    control: ControlPlaneDependency,
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[Workflow]:
    """Durable progress for operations that crossed external systems."""
    return control.workflow_history.list_workflows(limit)


@router.get("/v1/workflows/{workflow_id}", response_model=Workflow)
def get_workflow(
    workflow_id: str,
    control: ControlPlaneDependency,
) -> Workflow:
    return control.workflow_history.get(workflow_id)


@router.get("/v1/deployments/{deployment_id}", response_model=Deployment)
def deployment(
    deployment_id: str,
    control: ControlPlaneDependency,
) -> Deployment:
    return control.deployments.get_deployment(deployment_id)


@router.post(
    "/v1/drift", response_model=Deployment, status_code=status.HTTP_202_ACCEPTED
)
def check_drift(
    request: DriftRequest,
    operator: OperatorDependency,
    control: ControlPlaneDependency,
) -> Deployment:
    return control.deployments.submit_deployment(
        operator, check=True, host_limit=request.host_limit
    )


@router.get("/v1/deployments/{deployment_id}/drift", response_model=DriftReport)
def drift_report(
    deployment_id: str,
    control: ControlPlaneDependency,
) -> DriftReport:
    return control.deployments.drift_report(deployment_id)


@router.post(
    "/v1/rollbacks",
    response_model=Deployment,
    status_code=status.HTTP_202_ACCEPTED,
)
def rollback(
    request: RollbackRequest,
    operator: OperatorDependency,
    control: ControlPlaneDependency,
) -> Deployment:
    return control.deployments.submit_rollback(
        operator, request.deployment_id, check=request.check
    )
