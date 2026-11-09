from typing import Annotated

from dishka import FromDishka
from dishka.integrations.fastapi import DishkaSyncRoute
from fastapi import APIRouter, Depends, File, Query, Response, UploadFile, status

from blitzecdn.capabilities.jobs import OperationService
from blitzecdn.capabilities.tls.certificates.api.models import (
    CertificateInfo,
    CertificateRequest,
    CertificateStatus,
    PreflightReport,
    RenewRequest,
)
from blitzecdn.capabilities.tls.certificates.service import CertificateService
from blitzecdn.capabilities.tls.certificates.service.operations import (
    RECONCILE_DEDUPE_KEY,
    RECONCILE_OPERATION,
    RENEW_DEDUPE_KEY,
    RENEW_OPERATION,
    REQUEST_OPERATION,
    reconcile_payload,
    renew_payload,
    request_dedupe_key,
    request_payload,
)
from blitzecdn.entrypoints.api.dependencies import (
    OperatorDependency,
    require_operator,
)
from blitzecdn.entrypoints.api.models import Operation, as_operation, as_queued
from blitzecdn.entrypoints.api.pagination import Page, paginate
from blitzecdn.entrypoints.api.responses import (
    ACCEPTED_RESPONSE,
    polling_headers,
)

router = APIRouter(
    route_class=DishkaSyncRoute, dependencies=[Depends(require_operator)]
)


@router.get("/v3/hosts/{name}/certificate", response_model=CertificateInfo)
def certificate(
    name: str, certificates: FromDishka[CertificateService]
) -> CertificateInfo:
    return as_operation(certificates.certificate(name), CertificateInfo)


@router.get("/v3/certificates", response_model=Page[CertificateStatus])
def list_certificates(
    certificates: FromDishka[CertificateService],
    expiring_in: Annotated[int | None, Query(ge=0, le=3650)] = None,
) -> Page[CertificateStatus]:
    """Managed certificates against the clock, soonest expiry first."""
    if expiring_in is None:
        statuses = certificates.certificate_statuses()
    else:
        statuses = certificates.expiring_certificates(expiring_in)
    return paginate(statuses, lambda item: as_operation(item, CertificateStatus))


@router.post(
    "/v3/certificates/renew",
    response_model=Operation,
    status_code=status.HTTP_202_ACCEPTED,
    responses={202: ACCEPTED_RESPONSE},
)
def renew_certificates(
    response: Response,
    request: RenewRequest,
    operator: OperatorDependency,
    certificates: FromDishka[CertificateService],
    queued_operations: FromDishka[OperationService],
) -> Operation:
    """Queue reissuing ACME certificates due renewal; the result is a RenewalResult.

    The sweep stops between sites once the configured renewal budget is spent
    and reports the rest as skipped. One sweep runs at a time.
    """
    certificates.admit_renewal(sites=request.sites)
    return _accepted(
        response,
        queued_operations.submit(
            RENEW_OPERATION,
            renew_payload(
                operator,
                within_days=request.within_days,
                force=request.force,
                sites=request.sites,
            ),
            dedupe_key=RENEW_DEDUPE_KEY,
        ),
    )


@router.post(
    "/v3/certificates/reconcile",
    response_model=Operation,
    status_code=status.HTTP_202_ACCEPTED,
    responses={202: ACCEPTED_RESPONSE},
)
def reconcile_certificates(
    response: Response,
    operator: OperatorDependency,
    queued_operations: FromDishka[OperationService],
) -> Operation:
    """Queue issuing ready first certificates; the result is a ReconciliationResult.

    Every waiting site is checked, even one the scheduled run is backing off.
    A result naming a ``deployment`` has queued it: poll
    ``GET /v3/deployments/{id}`` for its outcome.
    """
    return _accepted(
        response,
        queued_operations.submit(
            RECONCILE_OPERATION,
            reconcile_payload(operator),
            dedupe_key=RECONCILE_DEDUPE_KEY,
        ),
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


@router.post(
    "/v3/hosts/{name}/certificate/request",
    response_model=Operation,
    status_code=status.HTTP_202_ACCEPTED,
    responses={202: ACCEPTED_RESPONSE},
)
def request_certificate(
    response: Response,
    name: str,
    request: CertificateRequest,
    operator: OperatorDependency,
    certificates: FromDishka[CertificateService],
    queued_operations: FromDishka[OperationService],
) -> Operation:
    """Queue issuing this site's certificate; the result is a CertificateInfo.

    Preflight runs in the job: a blocked check fails the operation, with the
    reason in ``error``. One request per site runs at a time.
    """
    certificates.admit_request(name, request.email)
    return _accepted(
        response,
        queued_operations.submit(
            REQUEST_OPERATION,
            request_payload(
                operator,
                name=name,
                email=request.email,
                skip_preflight=request.skip_preflight,
            ),
            dedupe_key=request_dedupe_key(name),
        ),
    )


@router.get("/v3/hosts/{name}/certificate/preflight", response_model=PreflightReport)
def certificate_preflight(
    name: str, certificates: FromDishka[CertificateService]
) -> PreflightReport:
    """Whether HTTP-01 could validate this site right now."""
    return as_operation(certificates.certificate_preflight(name), PreflightReport)


def _accepted(response: Response, job: object) -> Operation:
    queued = as_queued(job)
    polling_headers(response, active=True, location=f"/v3/operations/{queued.id}")
    return queued
