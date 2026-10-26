"""Cloudflare-compatible Automatic SSL/TLS operations."""

from dishka import FromDishka
from dishka.integrations.fastapi import DishkaSyncRoute
from fastapi import APIRouter, Depends

from blitzecdn.capabilities.tls.api.models import SslAutomaticReconciliation
from blitzecdn.capabilities.tls.automatic_ssl.service import AutomaticSslService
from blitzecdn.entrypoints.api.dependencies import (
    OperatorDependency,
    require_operator,
)
from blitzecdn.entrypoints.api.models import as_operation

router = APIRouter(
    route_class=DishkaSyncRoute, dependencies=[Depends(require_operator)]
)


@router.post(
    "/v1/ssl/automatic/reconcile",
    response_model=SslAutomaticReconciliation,
)
def reconcile_automatic_ssl(
    operator: OperatorDependency, automatic_ssl: FromDishka[AutomaticSslService]
) -> SslAutomaticReconciliation:
    """Probe origins from every edge and apply upgrade-only recommendations."""
    return as_operation(
        automatic_ssl.reconcile(operator),
        SslAutomaticReconciliation,
    )
