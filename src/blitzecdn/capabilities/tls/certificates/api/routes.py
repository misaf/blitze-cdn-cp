from typing import Annotated

from dishka import FromDishka
from dishka.integrations.fastapi import DishkaSyncRoute
from fastapi import APIRouter, Depends, File, Query, Response, UploadFile, status

from blitzecdn.capabilities.jobs import Job, OperationService
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
    RENEW_DEDUPE_KEY,
    CertificateReconcileJob,
    CertificateRenewalJob,
    CertificateRequestJob,
    request_dedupe_key,
)
from blitzecdn.core.ports.idempotency import IdempotencyKeyStore
from blitzecdn.delivery.api.dependencies import (
    OperatorDependency,
    require_operator,
)
from blitzecdn.delivery.api.errors import (
    BODY_RESPONSES,
    BUSY_RESPONSES,
    CONFLICT_RESPONSES,
    NOT_FOUND_RESPONSES,
    OPERATOR_RESPONSES,
)
from blitzecdn.delivery.api.idempotency import (
    IdempotencyKeyDependency,
    idempotent,
)
from blitzecdn.delivery.api.models import Operation, as_operation, as_queued
from blitzecdn.delivery.api.pagination import Page, paginate
from blitzecdn.delivery.api.responses import (
    ACCEPTED_RESPONSE,
    polling_headers,
)

router = APIRouter(
    route_class=DishkaSyncRoute,
    dependencies=[Depends(require_operator)],
    responses=OPERATOR_RESPONSES,
)


@router.get(
    "/v3/sites/{site}/certificate",
    response_model=CertificateInfo,
    responses=NOT_FOUND_RESPONSES,
)
def certificate(
    site: str, certificates: FromDishka[CertificateService]
) -> CertificateInfo:
    return as_operation(certificates.certificate(site), CertificateInfo)


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
    responses={
        202: ACCEPTED_RESPONSE,
        **NOT_FOUND_RESPONSES,
        **CONFLICT_RESPONSES,
        **BODY_RESPONSES,
    },
)
def renew_certificates(
    response: Response,
    request: RenewRequest,
    operator: OperatorDependency,
    certificates: FromDishka[CertificateService],
    queued_operations: FromDishka[OperationService],
    idempotency: IdempotencyKeyDependency,
    keys: FromDishka[IdempotencyKeyStore],
) -> Operation:
    """Queue reissuing ACME certificates due renewal; the result is a RenewalResult.

    The sweep stops between sites once the configured renewal budget is spent
    and reports the rest as skipped. One sweep runs at a time.
    """

    def submit() -> Job:
        certificates.admit_renewal(sites=request.sites)
        return queued_operations.submit(
            CertificateRenewalJob(
                operator=operator,
                within_days=request.within_days,
                force=request.force,
                sites=None if request.sites is None else tuple(request.sites),
            ),
            dedupe_key=RENEW_DEDUPE_KEY,
        )

    return _accepted(
        response,
        idempotent(idempotency, keys, create=submit, replay=queued_operations.get),
    )


@router.post(
    "/v3/certificates/reconcile",
    response_model=Operation,
    status_code=status.HTTP_202_ACCEPTED,
    responses={202: ACCEPTED_RESPONSE, **CONFLICT_RESPONSES},
)
def reconcile_certificates(
    response: Response,
    operator: OperatorDependency,
    queued_operations: FromDishka[OperationService],
    idempotency: IdempotencyKeyDependency,
    keys: FromDishka[IdempotencyKeyStore],
) -> Operation:
    """Queue issuing ready first certificates; the result is a ReconciliationResult.

    Every waiting site is checked, even one the scheduled run is backing off.
    A result naming a ``deployment`` has queued it: poll
    ``GET /v3/deployments/{id}`` for its outcome.
    """
    return _accepted(
        response,
        idempotent(
            idempotency,
            keys,
            create=lambda: queued_operations.submit(
                CertificateReconcileJob(operator=operator),
                dedupe_key=RECONCILE_DEDUPE_KEY,
            ),
            replay=queued_operations.get,
        ),
    )


@router.put(
    "/v3/sites/{site}/certificate",
    response_model=CertificateInfo,
    responses={**NOT_FOUND_RESPONSES, **BUSY_RESPONSES, **BODY_RESPONSES},
)
def upload_certificate(
    site: str,
    operator: OperatorDependency,
    certificates: FromDishka[CertificateService],
    certificate: Annotated[UploadFile, File()],
    private_key: Annotated[UploadFile, File()],
) -> CertificateInfo:
    """Install an operator's certificate for this site and activate it.

    Synchronous, unlike the certificate actions: it writes controller state
    only, contacting neither ACME nor an edge. Edges pick it up on the next
    deployment.
    """
    return as_operation(
        certificates.upload_certificate(
            site,
            certificate.file.read(1_048_577),
            private_key.file.read(262_145),
            operator,
        ),
        CertificateInfo,
    )


@router.post(
    "/v3/sites/{site}/certificate/request",
    response_model=Operation,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        202: ACCEPTED_RESPONSE,
        **NOT_FOUND_RESPONSES,
        **CONFLICT_RESPONSES,
        **BODY_RESPONSES,
    },
)
def request_certificate(
    response: Response,
    site: str,
    request: CertificateRequest,
    operator: OperatorDependency,
    certificates: FromDishka[CertificateService],
    queued_operations: FromDishka[OperationService],
    idempotency: IdempotencyKeyDependency,
    keys: FromDishka[IdempotencyKeyStore],
) -> Operation:
    """Queue issuing this site's certificate; the result is a CertificateInfo.

    Preflight runs in the job: a blocked check fails the operation, with the
    reason in ``error``. One request per site runs at a time.
    """

    def submit() -> Job:
        certificates.admit_request(site, request.email)
        return queued_operations.submit(
            CertificateRequestJob(
                operator=operator,
                name=site,
                email=request.email,
                skip_preflight=request.skip_preflight,
            ),
            dedupe_key=request_dedupe_key(site),
        )

    return _accepted(
        response,
        idempotent(idempotency, keys, create=submit, replay=queued_operations.get),
    )


@router.get(
    "/v3/sites/{site}/certificate/preflight",
    response_model=PreflightReport,
    responses=NOT_FOUND_RESPONSES,
)
def certificate_preflight(
    site: str, certificates: FromDishka[CertificateService]
) -> PreflightReport:
    """Whether HTTP-01 could validate this site right now."""
    return as_operation(certificates.certificate_preflight(site), PreflightReport)


def _accepted(response: Response, job: object) -> Operation:
    queued = as_queued(job)
    polling_headers(response, active=True, location=f"/v3/operations/{queued.id}")
    return queued
