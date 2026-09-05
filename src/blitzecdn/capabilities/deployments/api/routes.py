from fastapi import APIRouter, Depends, Query, status

from blitzecdn.api.dependencies import (
    ControlPlaneDependency,
    OperatorDependency,
    require_operator,
)
from blitzecdn.api.models import as_operation
from blitzecdn.capabilities.deployments.api.models import (
    Deployment,
    DeployRequest,
    DriftReport,
    DriftRequest,
    RollbackRequest,
)

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
    return as_operation(
        control.deployments.submit_deployment(
            operator, check=request.check, host_limit=request.host_limit
        ),
        Deployment,
    )


@router.get("/v1/deployments", response_model=list[Deployment])
def deployments(
    control: ControlPlaneDependency,
    limit: int = Query(20, ge=1, le=100),
) -> list[Deployment]:
    return [
        as_operation(item, Deployment)
        for item in control.deployments.list_deployments(limit)
    ]


@router.get("/v1/deployments/{deployment_id}", response_model=Deployment)
def deployment(
    deployment_id: str,
    control: ControlPlaneDependency,
) -> Deployment:
    return as_operation(control.deployments.get_deployment(deployment_id), Deployment)


@router.post(
    "/v1/drift", response_model=Deployment, status_code=status.HTTP_202_ACCEPTED
)
def check_drift(
    request: DriftRequest,
    operator: OperatorDependency,
    control: ControlPlaneDependency,
) -> Deployment:
    return as_operation(
        control.deployments.submit_deployment(
            operator, check=True, host_limit=request.host_limit
        ),
        Deployment,
    )


@router.get("/v1/deployments/{deployment_id}/drift", response_model=DriftReport)
def drift_report(
    deployment_id: str,
    control: ControlPlaneDependency,
) -> DriftReport:
    return as_operation(control.deployments.drift_report(deployment_id), DriftReport)


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
    return as_operation(
        control.deployments.submit_rollback(
            operator, request.deployment_id, check=request.check
        ),
        Deployment,
    )
