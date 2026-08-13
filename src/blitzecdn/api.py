import asyncio
import functools
import hmac
import logging
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
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
    Response,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse, PlainTextResponse

from blitzecdn import __version__
from blitzecdn.api_models import (
    DeployRequest,
    DriftRequest,
    EdgeRemoval,
    OriginCheckRequest,
    PurgeRequest,
    RenewRequest,
    RollbackRequest,
    StatsRequest,
)
from blitzecdn.config import Settings
from blitzecdn.control_plane import ControlPlane, build_control_plane
from blitzecdn.domain.audit import AuditEvent
from blitzecdn.domain.cache import (
    CacheStatsReport,
    PurgeResult,
)
from blitzecdn.domain.certificates import (
    CertificateInfo,
    CertificateRequest,
    CertificateStatus,
    PreflightReport,
    ReconciliationResult,
    RenewalResult,
)
from blitzecdn.domain.deployments import Deployment, DriftReport
from blitzecdn.domain.dns import DnsRecord, Domain, RecordPatch, RecordType
from blitzecdn.domain.edges import Edge, EdgePatch
from blitzecdn.domain.operations import Workflow
from blitzecdn.domain.origins import OriginReport
from blitzecdn.domain.sites import CdnSite
from blitzecdn.exceptions import (
    BlitzeError,
    ConfigurationError,
    ConflictError,
    DeploymentBusyError,
    ExecutionError,
    NotFoundError,
)
from blitzecdn.scheduler import build_scheduler

_LOGGER = logging.getLogger(__name__)

#: How many recent deployments `/metrics` counts by status. Bounded because the
#: gauge is meant to answer "is the fleet converging" rather than to replay
#: history, which the deployments route already serves.
_METRICS_WINDOW = 100


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings.from_environment()
    control_plane: ControlPlane
    renewal_pool: ThreadPoolExecutor

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        nonlocal control_plane, renewal_pool
        control_plane = build_control_plane(resolved, pool_connections=True)
        application.state.control_plane = control_plane
        renewal_pool = ThreadPoolExecutor(
            max_workers=resolved.certificate_renewal_workers,
            thread_name_prefix="blitzecdn-renewal",
        )
        control_plane.deployments.initialize()
        control_plane.recovery.reconcile_interrupted()
        scheduler = build_scheduler(resolved)
        if scheduler is not None:
            scheduler.start()
        try:
            yield
        finally:
            if scheduler is not None:
                scheduler.shutdown(wait=True)
            renewal_pool.shutdown(wait=True)
            control_plane.close()

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
        _request: Request, x_api_key: str | None = Header(default=None)
    ) -> str:
        if not resolved.api_keys:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "API authentication is not configured",
            )
        if x_api_key:
            for operator, expected in resolved.api_keys.items():
                if hmac.compare_digest(x_api_key, expected.get_secret_value()):
                    return operator
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    operator_dependency = Annotated[str, Depends(require_operator)]

    # Starlette resolves a handler by walking the exception's MRO, so the most
    # specific registration below wins and `BlitzeError` stays the catch-all.
    #
    # The distinction is not cosmetic. Mapping the whole hierarchy to 503 told
    # every client that a permanent condition — a host limit matching no edge,
    # a certificate that does not cover the site, certbot not being installed —
    # was a transient one worth retrying, and told alerting the controller was
    # unavailable when it was working correctly and declining bad input.
    @application.exception_handler(NotFoundError)
    async def not_found_handler(_request: object, exc: NotFoundError) -> object:
        return _error_response(status.HTTP_404_NOT_FOUND, str(exc))

    @application.exception_handler(DeploymentBusyError)
    async def deployment_busy_handler(
        _request: object, exc: DeploymentBusyError
    ) -> object:
        # A conflict, but the one kind that does clear on its own, so it is the
        # one conflict that earns a Retry-After.
        return _error_response(
            status.HTTP_409_CONFLICT, str(exc), headers={"Retry-After": "30"}
        )

    @application.exception_handler(ConflictError)
    async def conflict_handler(_request: object, exc: ConflictError) -> object:
        return _error_response(status.HTTP_409_CONFLICT, str(exc))

    @application.exception_handler(ConfigurationError)
    async def configuration_error_handler(
        _request: object, exc: ConfigurationError
    ) -> object:
        # Something in the request or in the controller's own configuration is
        # wrong and will stay wrong until a person changes it. Retrying is
        # never the answer, so this must not look like 503.
        return _error_response(status.HTTP_400_BAD_REQUEST, str(exc))

    @application.exception_handler(ExecutionError)
    async def execution_error_handler(_request: object, exc: ExecutionError) -> object:
        # The controller worked; something it depends on — Ansible, certbot, an
        # edge — did not answer usefully. That is a bad gateway, not a
        # controller that is down.
        return _error_response(status.HTTP_502_BAD_GATEWAY, str(exc))

    @application.exception_handler(BlitzeError)
    async def application_error_handler(_request: object, exc: BlitzeError) -> object:
        return _error_response(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))

    @application.get("/health")
    def health(response: Response) -> dict[str, str]:
        """Whether this controller can actually answer, not merely respond.

        It used to return ok unconditionally, which made it a check on uvicorn
        rather than on the control plane: a database that had gone unreadable,
        or one on a schema this release refuses, still reported healthy to
        whatever was probing it. The cheapest honest question is whether
        persistence answers, so that is what this asks.

        503 rather than an error body, because the caller is a probe: it reads
        the status and nothing else.
        """
        try:
            control_plane.workflow_history.list_workflows(1)
        except Exception as exc:
            _LOGGER.exception("health check failed")
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "unavailable", "detail": type(exc).__name__}
        return {"status": "ok"}

    @application.get("/metrics", response_class=PlainTextResponse)
    def metrics(_operator: operator_dependency) -> str:
        """Prometheus text format, built from state the control plane already has.

        Deliberately not a metrics *library*: everything here is a gauge read
        at scrape time out of SQLite, so there are no counters to keep in
        memory, nothing to reset on restart, and no second source of truth
        about what happened. Authenticated like every other data route — the
        fleet size and which certificates are close to expiry are not public.
        """
        recent = control_plane.deployments.list_deployments(_METRICS_WINDOW)
        expiring = control_plane.certificates.expiring_certificates()
        unrenewable = len([status_ for status_ in expiring if not status_.renewable])
        by_status: dict[str, int] = {}
        for deployment in recent:
            by_status[deployment.status.value] = (
                by_status.get(deployment.status.value, 0) + 1
            )
        lines = [
            "# HELP blitzecdn_edges Registered edge servers.",
            "# TYPE blitzecdn_edges gauge",
            f"blitzecdn_edges {len(control_plane.edges.list_edges())}",
            "# HELP blitzecdn_sites Derived virtual hosts.",
            "# TYPE blitzecdn_sites gauge",
            f"blitzecdn_sites {len(control_plane.dns.list_sites())}",
            "# HELP blitzecdn_certificates_expiring Managed certificates near expiry.",
            "# TYPE blitzecdn_certificates_expiring gauge",
            f"blitzecdn_certificates_expiring {len(expiring)}",
            "# HELP blitzecdn_certificates_unrenewable Expiring but not reissuable.",
            "# TYPE blitzecdn_certificates_unrenewable gauge",
            f"blitzecdn_certificates_unrenewable {unrenewable}",
            "# HELP blitzecdn_deployments Deployments in the recent window, by status.",
            "# TYPE blitzecdn_deployments gauge",
        ]
        lines.extend(
            f'blitzecdn_deployments{{status="{state}"}} {by_status[state]}'
            for state in sorted(by_status)
        )
        return "\n".join(lines) + "\n"

    # Sites are derived from DNS records and are therefore read-only here.
    # Create, change, or remove a record instead; the site follows.
    @application.get("/v1/sites", response_model=list[CdnSite])
    def list_sites(_operator: operator_dependency) -> list[CdnSite]:
        return control_plane.dns.list_sites()

    @application.get("/v1/sites/{name}", response_model=CdnSite)
    def get_site(name: str, _operator: operator_dependency) -> CdnSite:
        """The fully resolved policy for one site, as handed to the edges."""
        return control_plane.dns.get_site(name)

    @application.get("/v1/domains", response_model=list[Domain])
    def list_domains(_operator: operator_dependency) -> list[Domain]:
        return control_plane.dns.list_domains()

    @application.post(
        "/v1/domains", response_model=Domain, status_code=status.HTTP_201_CREATED
    )
    def create_domain(domain: Domain, operator: operator_dependency) -> Domain:
        return control_plane.dns.create_domain(domain, operator)

    @application.delete("/v1/domains/{domain}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_domain(domain: str, operator: operator_dependency) -> None:
        control_plane.dns.delete_domain(domain, operator)

    @application.get("/v1/domains/{domain}/records", response_model=list[DnsRecord])
    def list_records(domain: str, _operator: operator_dependency) -> list[DnsRecord]:
        return control_plane.dns.list_records(domain)

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
        return control_plane.dns.create_record(record, operator)

    # The proxy switch is a PATCH of {"proxied": true|false} on this route.
    @application.patch("/v1/domains/{domain}/records/{name}", response_model=DnsRecord)
    def update_record(
        domain: str,
        name: str,
        patch: RecordPatch,
        operator: operator_dependency,
        type_: Annotated[RecordType, Query(alias="type")] = RecordType.A,
    ) -> DnsRecord:
        return control_plane.dns.update_record(domain, name, type_, patch, operator)

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
        control_plane.dns.delete_record(domain, name, type_, operator)

    @application.get("/v1/dns/export")
    def dns_export(_operator: operator_dependency) -> list[dict[str, object]]:
        return control_plane.dns.dns_export()

    @application.get("/v1/sites/{name}/certificate", response_model=CertificateInfo)
    def certificate(name: str, _operator: operator_dependency) -> CertificateInfo:
        return control_plane.certificates.certificate(name)

    # Edges are the fleet, and the Ansible inventory is derived from them: the
    # `blitzecdn` inventory plugin reads these same rows at the start of every
    # run. So a write here is what puts a host into — or takes it out of — the
    # next deployment, and there is no inventory file to publish afterwards.
    @application.get("/v1/edges", response_model=list[Edge])
    def list_edges(_operator: operator_dependency) -> list[Edge]:
        """Every registered edge, which is exactly what Ansible will be given."""
        return control_plane.edges.list_edges()

    @application.get("/v1/edges/{name}", response_model=Edge)
    def get_edge(name: str, _operator: operator_dependency) -> Edge:
        return control_plane.edges.get_edge(name)

    @application.post(
        "/v1/edges", response_model=Edge, status_code=status.HTTP_201_CREATED
    )
    def add_edge(edge: Edge, operator: operator_dependency) -> Edge:
        """Register an edge. Nothing is converged and no host is contacted.

        201 rather than 202: this creates a record and returns having finished,
        unlike the deployment routes. The edge starts existing now and joins the
        fleet on the next `POST /v1/deployments`.
        """
        return control_plane.edges.add_edge(edge, operator)

    @application.patch("/v1/edges/{name}", response_model=Edge)
    def update_edge(name: str, patch: EdgePatch, operator: operator_dependency) -> Edge:
        """Change connection details or public addresses.

        The name is not patchable — certificates, audit history and deployment
        host limits all refer to it, so a rename would orphan them silently.
        `EdgePatch` has no such field, which makes that a 422 rather than a
        surprise.

        `public_addresses` and `ssh_sources` replace their whole list; sending
        `[]` clears them. Merging would make "remove the last entry"
        inexpressible.
        """
        return control_plane.edges.update_edge(name, patch, operator)

    @application.delete("/v1/edges/{name}", response_model=EdgeRemoval)
    def remove_edge(
        name: str,
        operator: operator_dependency,
        decommission: bool = Query(
            True,
            description=(
                "Strip BlitzeCDN configuration and TLS private keys from the "
                "host before deregistering it."
            ),
        ),
        force: bool = Query(
            False,
            description=(
                "Deregister even if the teardown failed. For a host that no "
                "longer exists; on one that is merely unreachable this leaves "
                "its configuration and private keys in place."
            ),
        ),
    ) -> EdgeRemoval:
        """Tear the host down, then deregister it — in that order.

        The order is the whole point. The inventory is derived from these rows,
        so deregistering first leaves a host no playbook can address again,
        still serving its last converged virtual hosts and still holding their
        private keys.

        Answers inline rather than queueing, following the purge and stats
        routes: this runs one playbook against one host, which is a far smaller
        job than the fleet-wide operations those already serve synchronously. It
        is nonetheless bounded by `deployment_timeout_seconds`, so a caller
        should use a client timeout to match.

        Returns 200 with the teardown result rather than 204. A forced removal
        that tore nothing down is still a success by the caller's own
        instruction, and the only place that distinction survives is this body —
        `hosts` is empty when no edge reported, and carries the failed tasks
        when one did.
        """
        if not decommission:
            control_plane.edges.remove_edge(name, operator)
            return EdgeRemoval(name=name, decommissioned=False)
        hosts = control_plane.edges.decommission_edge(name, operator, force=force)
        return EdgeRemoval(name=name, decommissioned=True, hosts=hosts)

    @application.post("/v1/origins/check", response_model=OriginReport)
    def check_origins(
        request: OriginCheckRequest, operator: operator_dependency
    ) -> OriginReport:
        """Ask the edges to connect to the origins they proxy to.

        A POST despite changing nothing, following the cache-statistics route:
        it runs a playbook across the fleet, which is neither cacheable nor
        safe to retry blindly the way GET promises. It was a GET while the
        controller answered for itself; the edges answer now, because they are
        the machines that carry the traffic and an origin that allow-lists them
        refuses the controller while working perfectly.

        Answers inline, bounded by `deployment_timeout_seconds`, so a caller
        should use a client timeout to match.
        """
        return control_plane.edges.check_origins(
            operator, host_limit=request.host_limit
        )

    @application.post(
        "/v1/cache/purge",
        response_model=PurgeResult,
        responses={
            status.HTTP_409_CONFLICT: {
                "model": PurgeResult,
                "description": (
                    "Some edge did not purge. The body is the same PurgeResult "
                    "as a success, with complete=false and failed_hosts naming "
                    "the edges that may still be serving the cached copy."
                ),
            }
        },
    )
    def purge_cache(
        request: PurgeRequest, operator: operator_dependency, response: Response
    ) -> PurgeResult:
        """Remove cached responses from the edges.

        Answers inline rather than queueing: a purge is usually made under time
        pressure and the caller needs to know whether every edge actually
        dropped the object. Responds 409 when some edge did not — a partial
        purge means clients get different answers depending on which edge they
        reach, and that must not read as success.

        The 409 carries the whole result rather than an error string. A partial
        purge is the case where the caller most needs the detail — which edges
        purged, which did not, what each one reported — and raising here would
        have replaced all of it with prose to be parsed under time pressure.
        The status code says "not done"; the body says what to retry.
        """
        result = control_plane.cache.purge_cache(
            operator,
            entries=request.entries,
            purge_all=request.purge_all,
            host_limit=request.host_limit,
        )
        if not result.complete:
            response.status_code = status.HTTP_409_CONFLICT
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
        return control_plane.cache.cache_stats(operator, host_limit=request.host_limit)

    @application.get("/v1/certificates", response_model=list[CertificateStatus])
    def list_certificates(
        _operator: operator_dependency,
        expiring_in: int | None = Query(None, ge=0, le=3650),
    ) -> list[CertificateStatus]:
        """Managed certificates against the clock, soonest expiry first."""
        if expiring_in is None:
            return control_plane.certificates.certificate_statuses()
        return control_plane.certificates.expiring_certificates(expiring_in)

    @application.post("/v1/certificates/renew", response_model=RenewalResult)
    async def renew_certificates(
        request: RenewRequest, operator: operator_dependency
    ) -> RenewalResult:
        """Reissue ACME certificates close to expiry.

        Answers inline, but off the server's own thread pool and under a
        wall-clock budget. A sweep runs certbot per site and touches no edge
        configuration, so there is nothing to converge and nothing to poll —
        but there is also nothing bounding how long it takes, and a request
        that holds a shared worker for an hour is how one slow CA takes the
        whole API down with it.

        Sites the budget does not reach come back under ``skipped`` and are
        picked up by the next run, so a truncated sweep is a slower renewal
        rather than a missed one.
        """
        return await asyncio.get_running_loop().run_in_executor(
            renewal_pool,
            functools.partial(
                control_plane.certificates.renew_certificates,
                operator,
                within_days=request.within_days,
                force=request.force,
                sites=request.sites,
                budget_seconds=resolved.certificate_renewal_budget_seconds,
            ),
        )

    @application.post("/v1/certificates/reconcile", response_model=ReconciliationResult)
    def reconcile_certificates(operator: operator_dependency) -> ReconciliationResult:
        """Issue first certificates for ready sites, then install them.

        The same operation the scheduler runs on
        `certificate_reconcile_interval_seconds`, exposed so it can also be
        asked for. It was reachable from the CLI and from the timer but not
        over HTTP, which left API-driven operators with no way to say "do it
        now" after fixing whatever was blocking preflight.

        Eligibility is re-checked under the deployment lock, so this is safe to
        call while the scheduler is running: only one of them can still see a
        site as unissued and contact the CA.
        """
        return control_plane.certificates.reconcile_certificates(operator)

    @application.post(
        "/v1/sites/{name}/certificate/upload", response_model=CertificateInfo
    )
    async def upload_certificate(
        name: str,
        operator: operator_dependency,
        certificate: Annotated[UploadFile, File()],
        private_key: Annotated[UploadFile, File()],
    ) -> CertificateInfo:
        return control_plane.certificates.upload_certificate(
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
        return control_plane.certificates.request_certificate(
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
        return control_plane.certificates.certificate_preflight(name)

    @application.post(
        "/v1/deployments",
        response_model=Deployment,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def deploy(request: DeployRequest, operator: operator_dependency) -> Deployment:
        """Queue a convergence; poll GET /v1/deployments/{id} for the outcome."""
        return control_plane.deployments.submit_deployment(
            operator, check=request.check, host_limit=request.host_limit
        )

    @application.get("/v1/deployments", response_model=list[Deployment])
    def deployments(
        _operator: operator_dependency, limit: int = Query(20, ge=1, le=100)
    ) -> list[Deployment]:
        return control_plane.deployments.list_deployments(limit)

    @application.get("/v1/workflows", response_model=list[Workflow])
    def list_workflows(
        _operator: operator_dependency,
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> list[Workflow]:
        """Durable progress for operations that crossed external systems."""
        return control_plane.workflow_history.list_workflows(limit)

    @application.get("/v1/workflows/{workflow_id}", response_model=Workflow)
    def get_workflow(workflow_id: str, _operator: operator_dependency) -> Workflow:
        return control_plane.workflow_history.get(workflow_id)

    @application.get("/v1/deployments/{deployment_id}", response_model=Deployment)
    def deployment(deployment_id: str, _operator: operator_dependency) -> Deployment:
        return control_plane.deployments.get_deployment(deployment_id)

    @application.post(
        "/v1/drift", response_model=Deployment, status_code=status.HTTP_202_ACCEPTED
    )
    def check_drift(request: DriftRequest, operator: operator_dependency) -> Deployment:
        """Queue a check-mode run; read the answer from the drift route below.

        Queued rather than answered inline for the same reason a deploy is: a
        full check can take as long as `deployment_timeout_seconds`, which no
        HTTP client will wait for.
        """
        return control_plane.deployments.submit_deployment(
            operator, check=True, host_limit=request.host_limit
        )

    @application.get(
        "/v1/deployments/{deployment_id}/drift", response_model=DriftReport
    )
    def drift_report(deployment_id: str, _operator: operator_dependency) -> DriftReport:
        """Read any completed check-mode deployment as a drift report."""
        return control_plane.deployments.drift_report(deployment_id)

    @application.post(
        "/v1/rollbacks",
        response_model=Deployment,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def rollback(request: RollbackRequest, operator: operator_dependency) -> Deployment:
        """Queue a rollback; poll GET /v1/deployments/{id} for the outcome."""
        return control_plane.deployments.submit_rollback(
            operator, request.deployment_id, check=request.check
        )

    @application.get("/v1/audit-events", response_model=list[AuditEvent])
    def audit_events(
        _operator: operator_dependency, limit: int = Query(100, ge=1, le=500)
    ) -> list[AuditEvent]:
        return control_plane.audit.list_audit_events(limit)

    return application


def _error_response(
    status_code: int, detail: str, headers: dict[str, str] | None = None
) -> object:
    return JSONResponse(
        status_code=status_code, content={"detail": detail}, headers=headers
    )
