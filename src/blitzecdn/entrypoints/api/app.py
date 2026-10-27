"""FastAPI application composition for the BlitzeCDN control plane."""

from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, asynccontextmanager

from dishka.integrations.starlette import SyncContainerMiddleware
from fastapi import FastAPI, status
from fastapi.responses import JSONResponse
from fastapi_pagination import add_pagination
from pydantic import ValidationError

from blitzecdn import __version__
from blitzecdn.composition import Adapters, ApiNode, api_container, load_registry
from blitzecdn.core.config import Settings
from blitzecdn.core.exceptions import BlitzeError, FailureKind, classify
from blitzecdn.core.features import FeatureRegistry
from blitzecdn.entrypoints.api.access import AllowedIPsMiddleware


def create_app(
    settings: Settings | None = None, features: FeatureRegistry | None = None
) -> FastAPI:
    resolved = settings or Settings.from_environment()
    # Loaded once, for the routers and the lifespan alike; injectable for tests.
    registry = features if features is not None else load_registry()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        with ExitStack() as resources:
            container = api_container(
                resolved, Adapters(features=registry, pool_connections=True)
            )
            resources.callback(container.close)
            node = container.get(ApiNode)
            worker_pool = ThreadPoolExecutor(
                max_workers=resolved.api_worker_threads,
                thread_name_prefix="blitzecdn-worker",
            )
            # Drain in-flight work first; every resource is released even if a step
            # raises.
            resources.callback(node.stop)
            resources.callback(worker_pool.shutdown, wait=True)
            application.state.dishka_container = container
            application.state.worker_pool = worker_pool
            node.start()
            yield

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
        telemetry={
            "exclude": lambda scope: scope["path"] in {"/health", "/metrics"},
            # Exception messages can contain infrastructure credentials. Keep
            # exception logging local; export request spans and metrics only.
            "logs": False,
        },
        # One schema per model, set here as well as per model: with only the
        # per-model override, FastAPI still emitted both variants and pydantic's
        # remapping left dangling `$ref`s that validators and generators reject.
        separate_input_output_schemas=False,
    )
    application.state.settings = resolved
    # A scope per request over the container the lifespan builds; routes take
    # services as ``FromDishka[...]``. Added first, so the IP check wraps it.
    application.add_middleware(SyncContainerMiddleware)
    if resolved.allowed_ips:
        application.add_middleware(
            AllowedIPsMiddleware, allowed_ips=resolved.allowed_ips
        )

    _register_exception_handlers(application)
    # The application does not know which capabilities exist. Every router is a
    # feature's contribution, in roster order.
    for router in registry.api_routers():
        application.include_router(router)
    add_pagination(application)
    return application


#: How a failure reaches a client: `FailureKind` projected onto status codes,
#: as `entrypoints.cli.main` projects it onto exit codes. Total by
#: construction; `test_delivery_parity.py` catches a missing kind.
_HTTP_STATUS: dict[FailureKind, int] = {
    # `Retry-After` rides along below, because the header is the difference
    # between this and a plain conflict and is meaningless off it.
    FailureKind.BUSY: status.HTTP_409_CONFLICT,
    FailureKind.CONFLICT: status.HTTP_409_CONFLICT,
    FailureKind.NOT_FOUND: status.HTTP_404_NOT_FOUND,
    # The controller is fine; what it was driving is not. A gateway error says
    # that, where a 500 would blame this service for its dependency.
    FailureKind.EXECUTION: status.HTTP_502_BAD_GATEWAY,
    FailureKind.CONFIGURATION: status.HTTP_400_BAD_REQUEST,
    # Not 503: a feature that will not load will not come back on retry.
    FailureKind.INTERNAL: status.HTTP_500_INTERNAL_SERVER_ERROR,
}


def _register_exception_handlers(application: FastAPI) -> None:
    @application.exception_handler(ValidationError)
    async def validation_error_handler(
        _request: object, exc: ValidationError
    ) -> JSONResponse:
        # Cross-field errors found while merging a PATCH are still client errors.
        # A pydantic `ValidationError` is outside core's taxonomy.
        return _error_response(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))

    # One handler for the hierarchy; Starlette resolves by MRO and
    # `_HTTP_STATUS` picks the code.
    @application.exception_handler(BlitzeError)
    async def application_error_handler(
        _request: object, exc: BlitzeError
    ) -> JSONResponse:
        return _failure_response(exc)


def _failure_response(exc: BlitzeError) -> JSONResponse:
    kind = classify(exc)
    headers = {"Retry-After": "30"} if kind is FailureKind.BUSY else None
    return _error_response(_HTTP_STATUS[kind], str(exc), headers=headers)


def _error_response(
    status_code: int, detail: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content={"detail": detail}, headers=headers
    )
