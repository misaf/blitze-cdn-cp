from typing import Annotated

from dishka import FromDishka
from dishka.integrations.fastapi import DishkaSyncRoute
from fastapi import APIRouter, Depends, File, Query, UploadFile

from blitzecdn.capabilities.tls.api.models import (
    CertificateInfo,
    CertificateRequest,
    CertificateStatus,
    PreflightReport,
    ReconciliationResult,
    RenewalResult,
    RenewRequest,
)
from blitzecdn.capabilities.tls.certificates.service import CertificateService
from blitzecdn.capabilities.tls.settings import CertificateConfig
from blitzecdn.entrypoints.api.dependencies import (
    OperatorDependency,
    WorkerPoolDependency,
    require_operator,
)
from blitzecdn.entrypoints.api.models import as_operation

router = APIRouter(
    route_class=DishkaSyncRoute, dependencies=[Depends(require_operator)]
)


@router.get("/v3/hosts/{name}/certificate", response_model=CertificateInfo)
def certificate(
    name: str, certificates: FromDishka[CertificateService]
) -> CertificateInfo:
    return as_operation(certificates.certificate(name), CertificateInfo)


@router.get("/v3/certificates", response_model=list[CertificateStatus])
def list_certificates(
    certificates: FromDishka[CertificateService],
    expiring_in: int | None = Query(None, ge=0, le=3650),
) -> list[CertificateStatus]:
    """Managed certificates against the clock, soonest expiry first."""
    if expiring_in is None:
        statuses = certificates.certificate_statuses()
    else:
        statuses = certificates.expiring_certificates(expiring_in)
    return [as_operation(item, CertificateStatus) for item in statuses]


@router.post("/v3/certificates/renew", response_model=RenewalResult)
def renew_certificates(
    request: RenewRequest,
    operator: OperatorDependency,
    certificates: FromDishka[CertificateService],
    certificate_config: FromDishka[CertificateConfig],
    worker_pool: WorkerPoolDependency,
) -> RenewalResult:
    """Reissue ACME certificates close to expiry within a bounded worker pool."""
    renewal = worker_pool.submit(
        certificates.renew_certificates,
        operator,
        within_days=request.within_days,
        force=request.force,
        sites=request.sites,
        budget_seconds=certificate_config.renewal_budget_seconds,
    )
    return as_operation(renewal.result(), RenewalResult)


@router.post("/v3/certificates/reconcile", response_model=ReconciliationResult)
def reconcile_certificates(
    operator: OperatorDependency, certificates: FromDishka[CertificateService]
) -> ReconciliationResult:
    """Issue first certificates for ready sites and queue their deployment.

    Poll GET /v3/deployments/{id} for the queued deployment's outcome.
    """
    return as_operation(
        certificates.reconcile_certificates(operator),
        ReconciliationResult,
    )


@router.post("/v3/hosts/{name}/certificate/upload", response_model=CertificateInfo)
def upload_certificate(
    name: str,
    operator: OperatorDependency,
    certificates: FromDishka[CertificateService],
    certificate: Annotated[UploadFile, File()],
    private_key: Annotated[UploadFile, File()],
) -> CertificateInfo:
    return as_operation(
        certificates.upload_certificate(
            name,
            certificate.file.read(1_048_577),
            private_key.file.read(262_145),
            operator,
        ),
        CertificateInfo,
    )


@router.post("/v3/hosts/{name}/certificate/request", response_model=CertificateInfo)
def request_certificate(
    name: str,
    request: CertificateRequest,
    operator: OperatorDependency,
    certificates: FromDishka[CertificateService],
) -> CertificateInfo:
    return as_operation(
        certificates.request_certificate(
            name,
            operator,
            request.email,
            skip_preflight=request.skip_preflight,
        ),
        CertificateInfo,
    )


@router.get("/v3/hosts/{name}/certificate/preflight", response_model=PreflightReport)
def certificate_preflight(
    name: str, certificates: FromDishka[CertificateService]
) -> PreflightReport:
    """Whether HTTP-01 could validate this site right now."""
    return as_operation(certificates.certificate_preflight(name), PreflightReport)
