from dishka import FromDishka
from dishka.integrations.fastapi import DishkaSyncRoute
from fastapi import APIRouter, Depends, Response, status

from blitzecdn.capabilities.deployments.api.models import (
    Deployment,
    DeploymentDiff,
    DeploymentTarget,
    DeployRequest,
    DriftReport,
    RollbackRequest,
)
from blitzecdn.capabilities.deployments.service.convergence import DeploymentService
from blitzecdn.core.ports.idempotency import IdempotencyKeyStore
from blitzecdn.delivery.api.dependencies import (
    OperatorDependency,
    require_operator,
)
from blitzecdn.delivery.api.idempotency import (
    IdempotencyKeyDependency,
    idempotent,
)
from blitzecdn.delivery.api.models import as_operation
from blitzecdn.delivery.api.pagination import Page, PaginationParams, stored_page
from blitzecdn.delivery.api.responses import (
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
    idempotency: IdempotencyKeyDependency,
    keys: FromDishka[IdempotencyKeyStore],
) -> Deployment:
    """Queue a convergence; poll GET /v3/deployments/{id} for the outcome.

    With ``check`` it is a drift check: nothing changes, and
    ``GET /v3/deployments/{id}/drift`` reports what would have.
    """
    queued = as_operation(
        idempotent(
            idempotency,
            keys,
            create=lambda: deployments.submit_deployment(
                operator, check=request.check, host_limit=request.host_limit
            ),
            replay=deployments.get_deployment,
        ),
        Deployment,
    )
    polling_headers(response, active=True, location=f"/v3/deployments/{queued.id}")
    return queued


@router.get("/v3/deployments", response_model=Page[Deployment])
def deployments(
    deployments: FromDishka[DeploymentService],
    params: PaginationParams,
) -> Page[Deployment]:
    return stored_page(
        deployments.paginate_deployments(limit=params.limit, offset=params.offset),
        params,
        lambda item: as_operation(item, Deployment),
    )


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
    response_model=Page[DeploymentTarget],
)
def deployment_targets(
    deployment_id: str,
    deployments: FromDishka[DeploymentService],
    params: PaginationParams,
) -> Page[DeploymentTarget]:
    """How far each edge got in a rollout."""
    return stored_page(
        deployments.paginate_targets(
            deployment_id, limit=params.limit, offset=params.offset
        ),
        params,
        lambda target: DeploymentTarget.model_validate(
            target.model_dump(mode="json", exclude={"deployment_id", "fence"})
        ),
    )


@router.get("/v3/deployments/{deployment_id}/drift", response_model=DriftReport)
def drift_report(
    deployment_id: str,
    deployments: FromDishka[DeploymentService],
) -> DriftReport:
    return as_operation(deployments.drift_report(deployment_id), DriftReport)


@router.get("/v3/deployments/{deployment_id}/diff", response_model=DeploymentDiff)
def deployment_diff(
    deployment_id: str,
    deployments: FromDishka[DeploymentService],
) -> DeploymentDiff:
    """What this deployment changed on the edges, as a unified diff per file.

    Against the release the fleet was running when it was queued.
    """
    deployment = deployments.get_deployment(deployment_id)
    return as_operation(
        {
            "deployment_id": deployment.id,
            "previous_release_id": deployment.previous_release_id,
            "release_id": deployment.release_id,
            **deployments.diff(deployment_id).summary(),
        },
        DeploymentDiff,
    )


@router.post(
    "/v3/deployments/rollback",
    response_model=Deployment,
    status_code=status.HTTP_202_ACCEPTED,
    responses={202: ACCEPTED_RESPONSE},
)
def rollback(
    response: Response,
    request: RollbackRequest,
    operator: OperatorDependency,
    deployments: FromDishka[DeploymentService],
    idempotency: IdempotencyKeyDependency,
    keys: FromDishka[IdempotencyKeyStore],
) -> Deployment:
    """Queue reapplying an earlier deployment's release; it is a deployment too.

    Without ``deployment_id`` the target is the newest successful full-fleet
    deployment of a release other than the one the newest real run that reached
    any edge applied, so a partial rollout is repaired. Success adopts that
    release as desired state, discarding undeployed changes.
    """
    queued = as_operation(
        idempotent(
            idempotency,
            keys,
            create=lambda: deployments.submit_rollback(
                operator, request.deployment_id, check=request.check
            ),
            replay=deployments.get_deployment,
        ),
        Deployment,
    )
    polling_headers(response, active=True, location=f"/v3/deployments/{queued.id}")
    return queued
