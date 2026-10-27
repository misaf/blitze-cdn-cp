from typing import Annotated

from dishka import FromDishka
from dishka.integrations.fastapi import DishkaSyncRoute
from fastapi import APIRouter, Depends, Query, Response, status

from blitzecdn.capabilities.deployments.api.models import (
    Deployment,
    DeploymentTarget,
    DeployRequest,
    DriftReport,
    DriftRequest,
    RollbackRequest,
)
from blitzecdn.capabilities.deployments.service.convergence import DeploymentService
from blitzecdn.entrypoints.api.dependencies import (
    OperatorDependency,
    require_operator,
)
from blitzecdn.entrypoints.api.models import as_operation
from blitzecdn.entrypoints.api.responses import (
    ACCEPTED_RESPONSE,
    POLL_RESPONSE,
    polling_headers,
)

router = APIRouter(
    route_class=DishkaSyncRoute, dependencies=[Depends(require_operator)]
)


@router.post(
    "/v3/deployments",
    response_model=Deployment,
    status_code=status.HTTP_202_ACCEPTED,
    responses={202: ACCEPTED_RESPONSE},
)
def deploy(
    response: Response,
    request: DeployRequest,
    operator: OperatorDependency,
    deployments: FromDishka[DeploymentService],
) -> Deployment:
    """Queue a convergence; poll GET /v3/deployments/{id} for the outcome."""
    queued = as_operation(
        deployments.submit_deployment(
            operator, check=request.check, host_limit=request.host_limit
        ),
        Deployment,
    )
    polling_headers(response, active=True, location=f"/v3/deployments/{queued.id}")
    return queued


@router.get("/v3/deployments", response_model=list[Deployment])
def deployments(
    deployments: FromDishka[DeploymentService],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[Deployment]:
    return [
        as_operation(item, Deployment) for item in deployments.list_deployments(limit)
    ]


@router.get(
    "/v3/deployments/{deployment_id}",
    response_model=Deployment,
    responses={200: POLL_RESPONSE},
)
def deployment(
    response: Response,
    deployment_id: str,
    deployments: FromDishka[DeploymentService],
) -> Deployment:
    current = as_operation(deployments.get_deployment(deployment_id), Deployment)
    polling_headers(response, active=current.status in {"queued", "running"})
    return current


@router.get(
    "/v3/deployments/{deployment_id}/targets",
    response_model=list[DeploymentTarget],
)
def deployment_targets(
    deployment_id: str,
    deployments: FromDishka[DeploymentService],
) -> list[DeploymentTarget]:
    """How far each edge got, for watching a rollout or reading a partial failure."""
    return [
        as_operation(target, DeploymentTarget)
        for target in deployments.targets(deployment_id)
    ]


@router.post(
    "/v3/drift",
    response_model=Deployment,
    status_code=status.HTTP_202_ACCEPTED,
    responses={202: ACCEPTED_RESPONSE},
)
def check_drift(
    response: Response,
    request: DriftRequest,
    operator: OperatorDependency,
    deployments: FromDishka[DeploymentService],
) -> Deployment:
    queued = as_operation(
        deployments.submit_deployment(
            operator, check=True, host_limit=request.host_limit
        ),
        Deployment,
    )
    polling_headers(response, active=True, location=f"/v3/deployments/{queued.id}")
    return queued


@router.get("/v3/deployments/{deployment_id}/drift", response_model=DriftReport)
def drift_report(
    deployment_id: str,
    deployments: FromDishka[DeploymentService],
) -> DriftReport:
    return as_operation(deployments.drift_report(deployment_id), DriftReport)


@router.post(
    "/v3/rollbacks",
    response_model=Deployment,
    status_code=status.HTTP_202_ACCEPTED,
    responses={202: ACCEPTED_RESPONSE},
)
def rollback(
    response: Response,
    request: RollbackRequest,
    operator: OperatorDependency,
    deployments: FromDishka[DeploymentService],
) -> Deployment:
    queued = as_operation(
        deployments.submit_rollback(
            operator, request.deployment_id, check=request.check
        ),
        Deployment,
    )
    polling_headers(response, active=True, location=f"/v3/deployments/{queued.id}")
    return queued
