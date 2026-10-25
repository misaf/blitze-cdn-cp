import asyncio
import functools
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, UploadFile

from blitzecdn.api.dependencies import (
    OperatorDependency,
    ServicesDependency,
    WorkerPoolDependency,
    require_operator,
)
from blitzecdn.api.models import as_operation
from blitzecdn.capabilities.tls.api.models import (
    CertificateInfo,
    CertificateRequest,
    CertificateStatus,
    PreflightReport,
    ReconciliationResult,
    RenewalResult,
    RenewRequest,
)

router = APIRouter(dependencies=[Depends(require_operator)])


@router.get("/v1/hosts/{name}/certificate", response_model=CertificateInfo)
def certificate(name: str, control: ServicesDependency) -> CertificateInfo:
    return as_operation(control.certificates.certificate(name), CertificateInfo)


@router.get("/v1/certificates", response_model=list[CertificateStatus])
def list_certificates(
    control: ServicesDependency,
    expiring_in: int | None = Query(None, ge=0, le=3650),
) -> list[CertificateStatus]:
    """Managed certificates against the clock, soonest expiry first."""
    if expiring_in is None:
        statuses = control.certificates.certificate_statuses()
    else:
        statuses = control.certificates.expiring_certificates(expiring_in)
    return [as_operation(item, CertificateStatus) for item in statuses]


@router.post("/v1/certificates/renew", response_model=RenewalResult)
async def renew_certificates(
    request: RenewRequest,
    operator: OperatorDependency,
    control: ServicesDependency,
    worker_pool: WorkerPoolDependency,
) -> RenewalResult:
    """Reissue ACME certificates close to expiry within a bounded worker pool."""
    result = await asyncio.get_running_loop().run_in_executor(
        worker_pool,
        functools.partial(
            control.certificates.renew_certificates,
            operator,
            within_days=request.within_days,
            force=request.force,
            sites=request.sites,
            budget_seconds=control.certificate_config.renewal_budget_seconds,
        ),
    )
    return as_operation(result, RenewalResult)


@router.post("/v1/certificates/reconcile", response_model=ReconciliationResult)
def reconcile_certificates(
    operator: OperatorDependency, control: ServicesDependency
) -> ReconciliationResult:
    """Issue first certificates for ready sites and queue their deployment.

    Poll GET /v1/deployments/{id} for the queued deployment's outcome.
    """
    return as_operation(
        control.certificates.reconcile_certificates(operator),
        ReconciliationResult,
    )


@router.post("/v1/hosts/{name}/certificate/upload", response_model=CertificateInfo)
async def upload_certificate(
    name: str,
    operator: OperatorDependency,
    control: ServicesDependency,
    certificate: Annotated[UploadFile, File()],
    private_key: Annotated[UploadFile, File()],
) -> CertificateInfo:
    return as_operation(
        control.certificates.upload_certificate(
            name,
            await certificate.read(1_048_577),
            await private_key.read(262_145),
            operator,
        ),
        CertificateInfo,
    )


@router.post("/v1/hosts/{name}/certificate/request", response_model=CertificateInfo)
def request_certificate(
    name: str,
    request: CertificateRequest,
    operator: OperatorDependency,
    control: ServicesDependency,
) -> CertificateInfo:
    return as_operation(
        control.certificates.request_certificate(
            name,
            operator,
            request.email,
            skip_preflight=request.skip_preflight,
        ),
        CertificateInfo,
    )


@router.get("/v1/hosts/{name}/certificate/preflight", response_model=PreflightReport)
def certificate_preflight(name: str, control: ServicesDependency) -> PreflightReport:
    """Whether HTTP-01 could validate this site right now."""
    return as_operation(
        control.certificates.certificate_preflight(name), PreflightReport
    )
