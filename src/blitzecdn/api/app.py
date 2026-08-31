"""FastAPI application composition for the BlitzeCDN control plane."""

from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError

import blitzecdn.features.automatic_ssl.api.v1 as v1_automatic_ssl
import blitzecdn.features.automatic_ssl.api.v2 as v2_automatic_ssl
import blitzecdn.features.cache.api.v1 as v1_cache
import blitzecdn.features.cache.api.v2 as v2_cache
import blitzecdn.features.certificates.api.v1 as v1_certificates
import blitzecdn.features.certificates.api.v2 as v2_certificates
import blitzecdn.features.deployments.api.v1 as v1_deployments
import blitzecdn.features.deployments.api.v2 as v2_deployments
import blitzecdn.features.diagnostics.api.v1 as v1_diagnostics
import blitzecdn.features.diagnostics.api.v2 as v2_diagnostics
import blitzecdn.features.dns.api.v1 as v1_zones
import blitzecdn.features.dns.api.v1_sites as v1_sites
import blitzecdn.features.dns.api.v2 as v2_zones
import blitzecdn.features.dns.api.v2_sites as v2_sites
import blitzecdn.features.edges.api.v1 as v1_edges
import blitzecdn.features.edges.api.v2 as v2_edges
from blitzecdn import __version__
from blitzecdn.control_plane import build_control_plane
from blitzecdn.core.config import Settings
from blitzecdn.core.exceptions import (
    BlitzeError,
    ConfigurationError,
    ConflictError,
    DeploymentBusyError,
    ExecutionError,
    NotFoundError,
)
from blitzecdn.features.diagnostics.api import readiness as diagnostics
from blitzecdn.scheduler import build_scheduler


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings.from_environment()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        control = build_control_plane(resolved, pool_connections=True)
        renewal_pool = ThreadPoolExecutor(
            max_workers=resolved.certificate_renewal_workers,
            thread_name_prefix="blitzecdn-renewal",
        )
        application.state.control_plane = control
        application.state.renewal_pool = renewal_pool
        control.deployments.initialize()
        scheduler = build_scheduler(resolved)
        if scheduler is not None:
            scheduler.start()
        try:
            yield
        finally:
            if scheduler is not None:
                scheduler.shutdown(wait=True)
            renewal_pool.shutdown(wait=True)
            control.close()

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
    application.state.settings = resolved

    _register_exception_handlers(application)
    for router in (
        diagnostics.router,
        v1_diagnostics.router,
        v1_sites.router,
        v1_zones.router,
        v1_edges.router,
        v1_cache.router,
        v1_certificates.router,
        v1_automatic_ssl.router,
        v1_deployments.router,
        v2_diagnostics.router,
        v2_sites.router,
        v2_zones.router,
        v2_edges.router,
        v2_cache.router,
        v2_certificates.router,
        v2_automatic_ssl.router,
        v2_deployments.router,
    ):
        application.include_router(router)
    return application


def _register_exception_handlers(application: FastAPI) -> None:
    @application.exception_handler(ValidationError)
    async def validation_error_handler(
        _request: object, exc: ValidationError
    ) -> JSONResponse:
        # PATCH merges with current state inside the application service, so
        # some cross-field failures are discovered after request parsing. They
        # are still client validation errors rather than internal failures.
        return _error_response(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))

    @application.exception_handler(NotFoundError)
    async def not_found_handler(_request: object, exc: NotFoundError) -> JSONResponse:
        return _error_response(status.HTTP_404_NOT_FOUND, str(exc))

    @application.exception_handler(DeploymentBusyError)
    async def deployment_busy_handler(
        _request: object, exc: DeploymentBusyError
    ) -> JSONResponse:
        return _error_response(
            status.HTTP_409_CONFLICT, str(exc), headers={"Retry-After": "30"}
        )

    @application.exception_handler(ConflictError)
    async def conflict_handler(_request: object, exc: ConflictError) -> JSONResponse:
        return _error_response(status.HTTP_409_CONFLICT, str(exc))

    @application.exception_handler(ConfigurationError)
    async def configuration_error_handler(
        _request: object, exc: ConfigurationError
    ) -> JSONResponse:
        return _error_response(status.HTTP_400_BAD_REQUEST, str(exc))

    @application.exception_handler(ExecutionError)
    async def execution_error_handler(
        _request: object, exc: ExecutionError
    ) -> JSONResponse:
        return _error_response(status.HTTP_502_BAD_GATEWAY, str(exc))

    @application.exception_handler(BlitzeError)
    async def application_error_handler(
        _request: object, exc: BlitzeError
    ) -> JSONResponse:
        return _error_response(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))


def _error_response(
    status_code: int, detail: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content={"detail": detail}, headers=headers
    )
