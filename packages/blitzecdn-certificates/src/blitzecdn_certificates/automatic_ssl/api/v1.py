"""Cloudflare-compatible Automatic SSL/TLS operations."""

from fastapi import APIRouter, Depends

from blitzecdn.api.dependencies import (
    ControlPlaneDependency,
    OperatorDependency,
    require_operator,
)
from blitzecdn.api.operations import as_operation
from blitzecdn_certificates.api_models import SslAutomaticReconciliation
from blitzecdn_certificates.composition import build_automatic_ssl_service

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
        build_automatic_ssl_service(control).reconcile(operator),
        SslAutomaticReconciliation,
    )
