"""Cloudflare-compatible Automatic SSL/TLS operations."""

from dishka import FromDishka
from dishka.integrations.fastapi import DishkaSyncRoute
from fastapi import APIRouter, Depends

from blitzecdn.capabilities.automatic_ssl.api.models import SslAutomaticReconciliation
from blitzecdn.capabilities.automatic_ssl.service import AutomaticSslService
from blitzecdn.delivery.api.dependencies import (
    OperatorDependency,
    require_operator,
)
from blitzecdn.delivery.api.errors import (
    BUSY_RESPONSES,
    GATEWAY_RESPONSES,
    OPERATOR_RESPONSES,
)
from blitzecdn.delivery.api.models import as_operation

router = APIRouter(
    route_class=DishkaSyncRoute,
    dependencies=[Depends(require_operator)],
    responses=OPERATOR_RESPONSES,
)


@router.post(
    "/v3/ssl/automatic/reconcile",
    response_model=SslAutomaticReconciliation,
    responses={**BUSY_RESPONSES, **GATEWAY_RESPONSES},
)
def reconcile_automatic_ssl(
    operator: OperatorDependency, automatic_ssl: FromDishka[AutomaticSslService]
) -> SslAutomaticReconciliation:
    """Probe origins from every edge and apply upgrade-only recommendations."""
    return as_operation(
        automatic_ssl.reconcile(operator),
        SslAutomaticReconciliation,
    )
