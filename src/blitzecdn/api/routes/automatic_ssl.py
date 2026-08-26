"""Cloudflare-compatible Automatic SSL/TLS operations."""

from fastapi import APIRouter, Depends

from blitzecdn.api.dependencies import (
    ControlPlaneDependency,
    OperatorDependency,
    require_operator,
)
from blitzecdn.api.v2_operations import SslAutomaticReconciliation, as_v2

router = APIRouter(dependencies=[Depends(require_operator)])


@router.post(
    "/v1/ssl/automatic/reconcile",
    response_model=SslAutomaticReconciliation,
)
def reconcile_automatic_ssl(
    operator: OperatorDependency, control: ControlPlaneDependency
) -> SslAutomaticReconciliation:
    """Probe origins from every edge and apply upgrade-only recommendations."""
    return as_v2(control.automatic_ssl.reconcile(operator), SslAutomaticReconciliation)
