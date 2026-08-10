import hmac
import logging
import threading
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import (
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from blitzecdn import __version__
from blitzecdn.config import Settings
from blitzecdn.control_plane import build_control_plane
from blitzecdn.domain.models import (
    CERTIFICATE_RENEWAL_DAYS,
    EDGE_LIMIT,
    AuditEvent,
    CacheStatsReport,
    CdnSite,
    CertificateInfo,
    CertificateRequest,
    CertificateStatus,
    Deployment,
    DnsRecord,
    Domain,
    DriftReport,
    OriginCheck,
    PreflightReport,
    PurgeEntry,
    PurgeResult,
    RecordPatch,
    RecordType,
)
from blitzecdn.exceptions import BlitzeError, ConflictError, NotFoundError

_LOGGER = logging.getLogger(__name__)


class AuthThrottle:
    """Cap failed authentications per client so API keys cannot be brute forced.

    Behind a reverse proxy every request appears to come from the proxy, which
    makes this a coarse global backstop rather than a per-client budget; put
    real per-client limiting in the proxy.
    """

    def __init__(self, *, limit: int = 10, window_seconds: float = 60.0) -> None:
        self._limit = limit
        self._window = window_seconds
        self._lock = threading.Lock()
        self._failures: dict[str, deque[float]] = {}

    def allows(self, client: str) -> bool:
        with self._lock:
            return len(self._prune(client, time.monotonic())) < self._limit

    def record_failure(self, client: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._prune(client, now)
            self._failures.setdefault(client, deque()).append(now)

    def record_success(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)

    def _prune(self, client: str, now: float) -> deque[float]:
        failures = self._failures.get(client)
        if failures is None:
            return deque()
        cutoff = now - self._window
        while failures and failures[0] < cutoff:
            failures.popleft()
        if not failures:
            del self._failures[client]
        return failures


class DeployRequest(BaseModel):
    check: bool = False
    #: Narrow the run to some edges — a canary. Validated by the same pattern
    #: the CLI uses, so a rejected limit is a 422 rather than a queued
    #: deployment that fails once it reaches Ansible.
    host_limit: str | None = Field(
        default=None, max_length=512, pattern=EDGE_LIMIT.pattern
    )


class DriftRequest(BaseModel):
    host_limit: str | None = Field(
        default=None, max_length=512, pattern=EDGE_LIMIT.pattern
    )


class PurgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[PurgeEntry] = Field(default_factory=list, max_length=500)
    #: Empty the cache instead of removing named entries. Kept as its own flag
    #: rather than "no entries means everything" so a caller whose filter
    #: matched nothing cannot empty the fleet's cache by accident.
    purge_all: bool = False
    host_limit: str | None = Field(
        default=None, max_length=512, pattern=EDGE_LIMIT.pattern
    )


class StatsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host_limit: str | None = Field(
        default=None, max_length=512, pattern=EDGE_LIMIT.pattern
    )


class RenewRequest(BaseModel):
    within_days: int = Field(default=CERTIFICATE_RENEWAL_DAYS, ge=0, le=3650)
    force: bool = False
    #: Narrow the run to these sites. None means every managed certificate;
    #: an unknown name is a 404 rather than a quiet no-op.
    sites: list[str] | None = Field(default=None, min_length=1)


class RollbackRequest(BaseModel):
    deployment_id: str | None = Field(default=None, min_length=32, max_length=32)
    check: bool = False


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings.from_environment()
    control_plane = build_control_plane(resolved)
    throttle = AuthThrottle()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        control_plane.initialize()
        stop = threading.Event()
        reconciler: threading.Thread | None = None
        interval = resolved.certificate_reconcile_interval_seconds
        if interval:

            def reconcile_forever() -> None:
                while not stop.wait(interval):
                    try:
                        result = control_plane.reconcile_certificates("scheduler")
                        if result["issued"] or result["failed"]:
                            _LOGGER.info("certificate reconciliation: %s", result)
                    except Exception:
                        _LOGGER.exception("scheduled certificate reconciliation failed")

            reconciler = threading.Thread(
                target=reconcile_forever,
                name="blitzecdn-certificate-reconciler",
                daemon=True,
            )
            reconciler.start()
        try:
            yield
        finally:
            stop.set()
            if reconciler is not None:
                reconciler.join(timeout=1)

    application = FastAPI(
        title="BlitzeCDN control plane",
        version=__version__,
        description=(
            "Manage CDN sites, certificates, deployments, rollbacks, and audit "
            "history. Control endpoints require the X-API-Key header."
        ),
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    def require_operator(
        request: Request, x_api_key: str | None = Header(default=None)
    ) -> str:
        if not resolved.api_keys:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "API authentication is not configured",
            )
        client = request.client.host if request.client else "unknown"
        if not throttle.allows(client):
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "too many failed authentication attempts",
                headers={"Retry-After": "60"},
            )
        if x_api_key:
            for operator, expected in resolved.api_keys.items():
                if hmac.compare_digest(x_api_key, expected.get_secret_value()):
                    throttle.record_success(client)
                    return operator
        throttle.record_failure(client)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    operator_dependency = Annotated[str, Depends(require_operator)]

    @application.exception_handler(NotFoundError)
    async def not_found_handler(_request: object, exc: NotFoundError) -> object:
        return _error_response(status.HTTP_404_NOT_FOUND, str(exc))

    @application.exception_handler(ConflictError)
    async def conflict_handler(_request: object, exc: ConflictError) -> object:
        return _error_response(status.HTTP_409_CONFLICT, str(exc))

    @application.exception_handler(BlitzeError)
    async def application_error_handler(_request: object, exc: BlitzeError) -> object:
        return _error_response(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # Sites are derived from DNS records and are therefore read-only here.
    # Create, change, or remove a record instead; the site follows.
    @application.get("/v1/sites", response_model=list[CdnSite])
    def list_sites(_operator: operator_dependency) -> list[CdnSite]:
        return control_plane.repository.list_sites()

    @application.get("/v1/sites/{name}", response_model=CdnSite)
    def get_site(name: str, _operator: operator_dependency) -> CdnSite:
        """The fully resolved policy for one site, as handed to the edges."""
        return control_plane.repository.get_site(name)

    @application.get("/v1/domains", response_model=list[Domain])
    def list_domains(_operator: operator_dependency) -> list[Domain]:
        return control_plane.list_domains()

    @application.post(
        "/v1/domains", response_model=Domain, status_code=status.HTTP_201_CREATED
    )
    def create_domain(domain: Domain, operator: operator_dependency) -> Domain:
        return control_plane.create_domain(domain, operator)

    @application.delete("/v1/domains/{domain}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_domain(domain: str, operator: operator_dependency) -> None:
        control_plane.delete_domain(domain, operator)

    @application.get("/v1/domains/{domain}/records", response_model=list[DnsRecord])
    def list_records(domain: str, _operator: operator_dependency) -> list[DnsRecord]:
        return control_plane.list_records(domain)

    @application.post(
        "/v1/domains/{domain}/records",
        response_model=DnsRecord,
        status_code=status.HTTP_201_CREATED,
    )
    def create_record(
        domain: str, record: DnsRecord, operator: operator_dependency
    ) -> DnsRecord:
        if record.domain != domain:
            raise ConflictError(
                f"record domain {record.domain!r} does not match the path "
                f"segment {domain!r}"
            )
        return control_plane.create_record(record, operator)

    # The proxy switch is a PATCH of {"proxied": true|false} on this route.
    @application.patch("/v1/domains/{domain}/records/{name}", response_model=DnsRecord)
    def update_record(
        domain: str,
        name: str,
        patch: RecordPatch,
        operator: operator_dependency,
        type_: Annotated[RecordType, Query(alias="type")] = RecordType.A,
    ) -> DnsRecord:
        return control_plane.update_record(domain, name, type_, patch, operator)

    @application.delete(
        "/v1/domains/{domain}/records/{name}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def delete_record(
        domain: str,
        name: str,
        operator: operator_dependency,
        type_: Annotated[RecordType, Query(alias="type")] = RecordType.A,
    ) -> None:
        control_plane.delete_record(domain, name, type_, operator)

    @application.get("/v1/dns/export")
    def dns_export(_operator: operator_dependency) -> list[dict[str, object]]:
        return control_plane.dns_export()

    @application.get("/v1/sites/{name}/certificate", response_model=CertificateInfo)
    def certificate(name: str, _operator: operator_dependency) -> CertificateInfo:
        return control_plane.certificate(name)

    @application.get("/v1/origins/check", response_model=list[OriginCheck])
    def check_origins(_operator: operator_dependency) -> list[OriginCheck]:
        """Connect to every enabled site's origin the way the edge will.

        Bounded by `origin_check_timeout_seconds` per origin and probed in
        parallel, so this answers inline rather than needing a queued job.
        """
        return control_plane.check_origins()

    @application.post("/v1/cache/purge", response_model=PurgeResult)
    def purge_cache(
        request: PurgeRequest, operator: operator_dependency
    ) -> PurgeResult:
        """Remove cached responses from the edges.

        Answers inline rather than queueing: a purge is usually made under time
        pressure and the caller needs to know whether every edge actually
        dropped the object. Responds 409 when some edge did not — a partial
        purge means clients get different answers depending on which edge they
        reach, and that must not read as success.
        """
        result = control_plane.purge_cache(
            operator,
            entries=request.entries,
            purge_all=request.purge_all,
            host_limit=request.host_limit,
        )
        if not result.complete:
            raise ConflictError(
                "purged on "
                f"{len(result.succeeded)} of {len(result.hosts)} edges; "
                + ", ".join(host.host for host in result.failed)
                + " may still be serving the cached copy"
            )
        return result

    @application.post("/v1/cache/stats", response_model=CacheStatsReport)
    def cache_stats(
        request: StatsRequest, operator: operator_dependency
    ) -> CacheStatsReport:
        """Collect cache effectiveness from the edges.

        A POST despite reading nothing but counters: it runs a playbook across
        the fleet, which is neither cacheable nor safe to retry blindly the way
        GET promises.
        """
        return control_plane.cache_stats(operator, host_limit=request.host_limit)

    @application.get("/v1/certificates", response_model=list[CertificateStatus])
    def list_certificates(
        _operator: operator_dependency,
        expiring_in: int | None = Query(None, ge=0, le=3650),
    ) -> list[CertificateStatus]:
        """Managed certificates against the clock, soonest expiry first."""
        if expiring_in is None:
            return control_plane.certificate_statuses()
        return control_plane.expiring_certificates(expiring_in)

    @application.post("/v1/certificates/renew")
    def renew_certificates(
        request: RenewRequest, operator: operator_dependency
    ) -> dict[str, list[str]]:
        """Reissue ACME certificates close to expiry.

        Runs inline rather than on a worker: renewal is bounded by certbot's
        own timeout per site and touches no edge configuration, so there is no
        long convergence for a caller to poll.
        """
        return control_plane.renew_certificates(
            operator,
            within_days=request.within_days,
            force=request.force,
            sites=request.sites,
        )

    @application.post(
        "/v1/sites/{name}/certificate/upload", response_model=CertificateInfo
    )
    async def upload_certificate(
        name: str,
        operator: operator_dependency,
        certificate: Annotated[UploadFile, File()],
        private_key: Annotated[UploadFile, File()],
    ) -> CertificateInfo:
        return control_plane.upload_certificate(
            name,
            await certificate.read(1_048_577),
            await private_key.read(262_145),
            operator,
        )

    @application.post(
        "/v1/sites/{name}/certificate/request", response_model=CertificateInfo
    )
    def request_certificate(
        name: str,
        request: CertificateRequest,
        operator: operator_dependency,
    ) -> CertificateInfo:
        return control_plane.request_certificate(
            name,
            operator,
            request.email,
            skip_preflight=request.skip_preflight,
        )

    @application.get(
        "/v1/sites/{name}/certificate/preflight", response_model=PreflightReport
    )
    def certificate_preflight(
        name: str, _operator: operator_dependency
    ) -> PreflightReport:
        """Whether HTTP-01 could validate this site right now.

        Read-only and free of CA interaction, so it is safe to poll while a
        customer is still moving their DNS.
        """
        return control_plane.certificate_preflight(name)

    @application.post(
        "/v1/deployments",
        response_model=Deployment,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def deploy(request: DeployRequest, operator: operator_dependency) -> Deployment:
        """Queue a convergence; poll GET /v1/deployments/{id} for the outcome."""
        return control_plane.submit_deployment(
            operator, check=request.check, host_limit=request.host_limit
        )

    @application.get("/v1/deployments", response_model=list[Deployment])
    def deployments(
        _operator: operator_dependency, limit: int = Query(20, ge=1, le=100)
    ) -> list[Deployment]:
        return control_plane.repository.list_deployments(limit)

    @application.get("/v1/deployments/{deployment_id}", response_model=Deployment)
    def deployment(deployment_id: str, _operator: operator_dependency) -> Deployment:
        return control_plane.repository.get_deployment(deployment_id)

    @application.post(
        "/v1/drift", response_model=Deployment, status_code=status.HTTP_202_ACCEPTED
    )
    def check_drift(request: DriftRequest, operator: operator_dependency) -> Deployment:
        """Queue a check-mode run; read the answer from the drift route below.

        Queued rather than answered inline for the same reason a deploy is: a
        full check can take as long as `deployment_timeout_seconds`, which no
        HTTP client will wait for.
        """
        return control_plane.submit_deployment(
            operator, check=True, host_limit=request.host_limit
        )

    @application.get(
        "/v1/deployments/{deployment_id}/drift", response_model=DriftReport
    )
    def drift_report(deployment_id: str, _operator: operator_dependency) -> DriftReport:
        """Read any completed check-mode deployment as a drift report."""
        return control_plane.drift_report(deployment_id)

    @application.post(
        "/v1/rollbacks",
        response_model=Deployment,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def rollback(request: RollbackRequest, operator: operator_dependency) -> Deployment:
        """Queue a rollback; poll GET /v1/deployments/{id} for the outcome."""
        return control_plane.submit_rollback(
            operator, request.deployment_id, check=request.check
        )

    @application.get("/v1/audit-events", response_model=list[AuditEvent])
    def audit_events(
        _operator: operator_dependency, limit: int = Query(100, ge=1, le=500)
    ) -> list[AuditEvent]:
        return control_plane.repository.list_audit_events(limit)

    return application


def _error_response(status_code: int, detail: str) -> object:
    return JSONResponse(status_code=status_code, content={"detail": detail})
