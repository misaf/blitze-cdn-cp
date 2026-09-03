import asyncio
import functools
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, UploadFile

from blitzecdn.api.dependencies import (
    ControlPlaneDependency,
    OperatorDependency,
    WorkerPoolDependency,
    require_operator,
)
from blitzecdn.api.operations import as_operation
from blitzecdn_certificates.api.models import (
    CertificateInfo,
    CertificateRequest,
    CertificateStatus,
    PreflightReport,
    ReconciliationResult,
    RenewalResult,
    RenewRequest,
)
from blitzecdn_certificates.composition import (
    build_certificate_service,
    certificate_config,
)

router = APIRouter(dependencies=[Depends(require_operator)])


@router.get("/v1/sites/{name}/certificate", response_model=CertificateInfo)
def certificate(name: str, control: ControlPlaneDependency) -> CertificateInfo:
    return as_operation(
        build_certificate_service(control).certificate(name), CertificateInfo
    )


@router.get("/v1/certificates", response_model=list[CertificateStatus])
def list_certificates(
    control: ControlPlaneDependency,
    expiring_in: int | None = Query(None, ge=0, le=3650),
) -> list[CertificateStatus]:
    """Managed certificates against the clock, soonest expiry first."""
    if expiring_in is None:
        statuses = build_certificate_service(control).certificate_statuses()
    else:
        statuses = build_certificate_service(control).expiring_certificates(expiring_in)
    return [as_operation(item, CertificateStatus) for item in statuses]


@router.post("/v1/certificates/renew", response_model=RenewalResult)
async def renew_certificates(
    request: RenewRequest,
    operator: OperatorDependency,
    control: ControlPlaneDependency,
    worker_pool: WorkerPoolDependency,
) -> RenewalResult:
    """Reissue ACME certificates close to expiry within a bounded worker pool."""
    result = await asyncio.get_running_loop().run_in_executor(
        worker_pool,
        functools.partial(
            build_certificate_service(control).renew_certificates,
            operator,
            within_days=request.within_days,
            force=request.force,
            sites=request.sites,
            budget_seconds=certificate_config(control).renewal_budget_seconds,
        ),
    )
    return as_operation(result, RenewalResult)


@router.post("/v1/certificates/reconcile", response_model=ReconciliationResult)
def reconcile_certificates(
    operator: OperatorDependency, control: ControlPlaneDependency
) -> ReconciliationResult:
    """Issue first certificates for ready sites, then install them."""
    return as_operation(
        build_certificate_service(control).reconcile_certificates(operator),
        ReconciliationResult,
    )


@router.post("/v1/sites/{name}/certificate/upload", response_model=CertificateInfo)
async def upload_certificate(
    name: str,
    operator: OperatorDependency,
    control: ControlPlaneDependency,
    certificate: Annotated[UploadFile, File()],
    private_key: Annotated[UploadFile, File()],
) -> CertificateInfo:
    return as_operation(
        build_certificate_service(control).upload_certificate(
            name,
            await certificate.read(1_048_577),
            await private_key.read(262_145),
            operator,
        ),
        CertificateInfo,
    )


@router.post("/v1/sites/{name}/certificate/request", response_model=CertificateInfo)
def request_certificate(
    name: str,
    request: CertificateRequest,
    operator: OperatorDependency,
    control: ControlPlaneDependency,
) -> CertificateInfo:
    return as_operation(
        build_certificate_service(control).request_certificate(
            name,
            operator,
            request.email,
            skip_preflight=request.skip_preflight,
        ),
        CertificateInfo,
    )


@router.get("/v1/sites/{name}/certificate/preflight", response_model=PreflightReport)
def certificate_preflight(
    name: str, control: ControlPlaneDependency
) -> PreflightReport:
    """Whether HTTP-01 could validate this site right now."""
    return as_operation(
        build_certificate_service(control).certificate_preflight(name), PreflightReport
    )
