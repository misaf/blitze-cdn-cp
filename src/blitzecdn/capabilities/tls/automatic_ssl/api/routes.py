"""Cloudflare-compatible Automatic SSL/TLS operations."""

from fastapi import APIRouter, Depends

from blitzecdn.api.dependencies import (
    ControlPlaneDependency,
    OperatorDependency,
    require_operator,
)
from blitzecdn.api.models import as_operation
from blitzecdn.capabilities.tls.api.models import SslAutomaticReconciliation

router = APIRouter(dependencies=[Depends(require_operator)])


@router.post(
    "/v1/ssl/automatic/reconcile",
    response_model=SslAutomaticReconciliation,
)
def reconcile_automatic_ssl(
    operator: OperatorDependency, control: ControlPlaneDependency
) -> SslAutomaticReconciliation:
    """Probe origins from every edge and apply upgrade-only recommendations."""
    return as_operation(
        control.automatic_ssl.reconcile(operator),
        SslAutomaticReconciliation,
    )
