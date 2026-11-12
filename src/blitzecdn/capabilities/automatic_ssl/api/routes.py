"""Cloudflare-compatible Automatic SSL/TLS operations."""

from dishka import FromDishka
from dishka.integrations.fastapi import DishkaSyncRoute
from fastapi import APIRouter, Depends, Response, status

from blitzecdn.capabilities.automatic_ssl.service.operations import (
    RECONCILE_DEDUPE_KEY,
    AutomaticSslReconcileJob,
)
from blitzecdn.capabilities.jobs import OperationService
from blitzecdn.core.ports.idempotency import IdempotencyKeyStore
from blitzecdn.delivery.api.dependencies import (
    OperatorDependency,
    require_operator,
)
from blitzecdn.delivery.api.errors import CONFLICT_RESPONSES, OPERATOR_RESPONSES
from blitzecdn.delivery.api.idempotency import (
    IdempotencyKeyDependency,
    idempotent,
)
from blitzecdn.delivery.api.models import Operation, as_queued
from blitzecdn.delivery.api.responses import (
    ACCEPTED_RESPONSE,
    polling_headers,
)

router = APIRouter(
    route_class=DishkaSyncRoute,
    dependencies=[Depends(require_operator)],
    responses=OPERATOR_RESPONSES,
)


@router.post(
    "/v3/ssl/automatic/reconcile",
    response_model=Operation,
    status_code=status.HTTP_202_ACCEPTED,
    responses={202: ACCEPTED_RESPONSE, **CONFLICT_RESPONSES},
)
def reconcile_automatic_ssl(
    response: Response,
    operator: OperatorDependency,
    queued_operations: FromDishka[OperationService],
    idempotency: IdempotencyKeyDependency,
    keys: FromDishka[IdempotencyKeyStore],
) -> Operation:
    """Queue probing origins from every edge and applying upgrade-only
    recommendations; the result is an SslAutomaticReconciliation.

    One scan may be queued or running at a time. A result naming a
    ``deployment`` ran it: poll ``GET /v3/deployments/{id}`` for the record.
    """
    queued = as_queued(
        idempotent(
            idempotency,
            keys,
            create=lambda: queued_operations.submit(
                AutomaticSslReconcileJob(operator=operator),
                dedupe_key=RECONCILE_DEDUPE_KEY,
            ),
            replay=queued_operations.get,
        )
    )
    polling_headers(response, active=True, location=f"/v3/operations/{queued.id}")
    return queued
