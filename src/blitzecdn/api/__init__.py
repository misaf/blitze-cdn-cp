"""FastAPI application composition for the BlitzeCDN control plane."""

from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI, status
from fastapi.responses import JSONResponse

from blitzecdn import __version__
from blitzecdn.api.routes import (
    automatic_ssl,
    cache,
    certificates,
    deployments,
    diagnostics,
    edges,
    sites,
    zones,
)
from blitzecdn.config import Settings
from blitzecdn.control_plane import build_control_plane
from blitzecdn.exceptions import (
    BlitzeError,
    ConfigurationError,
    ConflictError,
    DeploymentBusyError,
    ExecutionError,
    NotFoundError,
)
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
        sites.router,
        zones.router,
        edges.router,
        cache.router,
        certificates.router,
        automatic_ssl.router,
        deployments.router,
    ):
        application.include_router(router)
    return application


def _register_exception_handlers(application: FastAPI) -> None:
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
