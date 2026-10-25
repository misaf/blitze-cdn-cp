from fastapi import APIRouter, Depends, Query, status

from blitzecdn.capabilities.deployments.api.models import (
    Deployment,
    DeploymentTarget,
    DeployRequest,
    DriftReport,
    DriftRequest,
    RollbackRequest,
)
from blitzecdn.entrypoints.api.dependencies import (
    OperatorDependency,
    ServicesDependency,
    require_operator,
)
from blitzecdn.entrypoints.api.models import as_operation

router = APIRouter(dependencies=[Depends(require_operator)])


@router.post(
    "/v1/deployments",
    response_model=Deployment,
    status_code=status.HTTP_202_ACCEPTED,
)
def deploy(
    request: DeployRequest,
    operator: OperatorDependency,
    control: ServicesDependency,
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
    control: ServicesDependency,
    limit: int = Query(20, ge=1, le=100),
) -> list[Deployment]:
    return [
        as_operation(item, Deployment)
        for item in control.deployments.list_deployments(limit)
    ]


@router.get("/v1/deployments/{deployment_id}", response_model=Deployment)
def deployment(
    deployment_id: str,
    control: ServicesDependency,
) -> Deployment:
    return as_operation(control.deployments.get_deployment(deployment_id), Deployment)


@router.get(
    "/v1/deployments/{deployment_id}/targets",
    response_model=list[DeploymentTarget],
)
def deployment_targets(
    deployment_id: str,
    control: ServicesDependency,
) -> list[DeploymentTarget]:
    """How far each edge got, for watching a rollout or reading a partial failure."""
    return [
        as_operation(target, DeploymentTarget)
        for target in control.deployments.targets(deployment_id)
    ]


@router.post(
    "/v1/drift", response_model=Deployment, status_code=status.HTTP_202_ACCEPTED
)
def check_drift(
    request: DriftRequest,
    operator: OperatorDependency,
    control: ServicesDependency,
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
    control: ServicesDependency,
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
    control: ServicesDependency,
) -> Deployment:
    return as_operation(
        control.deployments.submit_rollback(
            operator, request.deployment_id, check=request.check
        ),
        Deployment,
    )
