"""FastAPI application composition for the BlitzeCDN control plane."""

from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from blitzecdn import __version__
from blitzecdn.api.access import AllowedIPsMiddleware
from blitzecdn.composition import (
    build_control_plane,
    build_scheduler,
    load_control_plane_plugins,
)
from blitzecdn.core.config import Settings
from blitzecdn.core.exceptions import BlitzeError, FailureKind, classify
from blitzecdn.core.plugins import PluginRegistry, ProcessKind


def create_app(
    settings: Settings | None = None, plugins: PluginRegistry | None = None
) -> FastAPI:
    resolved = settings or Settings.from_environment()
    # Discovered once and used twice: the routers below need no services, and
    # the control plane the lifespan builds is handed the same registry rather
    # than repeating discovery. `plugins` is injectable so a test can serve an
    # app built from exactly the plugins the test is about.
    registry = plugins if plugins is not None else load_control_plane_plugins()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        control = build_control_plane(
            resolved,
            pool_connections=True,
            process=ProcessKind.API,
            plugins=registry,
        )
        # Where a route puts work that would otherwise block the event loop
        # for minutes. Core's, and generic: an installed capability's route
        # asks for it through `WorkerPoolDependency` rather than creating one,
        # because a plugin has no way to own an application-scoped resource
        # from a registration hook.
        worker_pool = ThreadPoolExecutor(
            max_workers=resolved.api_worker_threads,
            thread_name_prefix="blitzecdn-worker",
        )
        application.state.control_plane = control
        application.state.worker_pool = worker_pool
        # Republishing queued deployments is a plugin's startup contribution
        # now, not a line here: what a process owes at startup is the plugin's
        # business, and `RuntimeContext.process` is how it knows this is the API.
        control.start()
        scheduler = build_scheduler(resolved, control.jobs)
        if scheduler is not None:
            scheduler.start()
        try:
            yield
        finally:
            if scheduler is not None:
                scheduler.shutdown(wait=True)
            worker_pool.shutdown(wait=True)
            control.stop()
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
        # One published component per model, rather than a validation and a
        # serialization variant of each. The representation bases already ask
        # for that per model with `json_schema_mode_override`, and asking for
        # it here as well is not belt-and-braces: with only the per-model
        # override, FastAPI still generated both variants, pydantic
        # disambiguated the identical pair as `...TaskResult-Input__1`, and its
        # definition remapping then collapsed the pair to `TaskResult` without
        # rewriting the references inside `HostRun` — leaving two dangling
        # `$ref`s in the published document that every schema validator and
        # client generator rejects.
        separate_input_output_schemas=False,
    )
    application.state.settings = resolved
    if resolved.allowed_ips:
        application.add_middleware(
            AllowedIPsMiddleware, allowed_ips=resolved.allowed_ips
        )

    _register_exception_handlers(application)
    # The application does not know which capabilities exist. Every router is a
    # plugin's contribution, in registration order, so a separately installed
    # package adds routes without a line changing here.
    for router in registry.api_routers():
        application.include_router(router)
    return application


#: How a failure reaches a client. The projection of core's `FailureKind` onto
#: this delivery layer; `blitzecdn.cli.main` projects the same kinds onto exit
#: codes. Both used to classify exceptions themselves and disagreed about the
#: unmapped case — see `FailureKind`.
#:
#: Total over `FailureKind` by construction: `_failure_response` subscripts it,
#: so a kind added to core without a line here is a `KeyError` the parity test
#: in `tests/contract/test_delivery_parity.py` raises before a release does.
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
    # Not 503. A plugin that will not load is not a service that is briefly
    # unavailable, and inviting a retry that cannot succeed is worse than
    # admitting the installation is broken.
    FailureKind.INTERNAL: status.HTTP_500_INTERNAL_SERVER_ERROR,
}


def _register_exception_handlers(application: FastAPI) -> None:
    @application.exception_handler(ValidationError)
    async def validation_error_handler(
        _request: object, exc: ValidationError
    ) -> JSONResponse:
        # PATCH merges with current state inside the application service, so
        # some cross-field failures are discovered after request parsing. They
        # are still client validation errors rather than internal failures.
        # Not part of core's taxonomy: a `ValidationError` is pydantic's, not a
        # `BlitzeError`, and the CLI answers it out of band for the same reason.
        return _error_response(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))

    # One handler for the whole hierarchy. Starlette resolves a handler by
    # walking the raised exception's MRO, so registering the base class catches
    # every subclass, and which status each one gets is `_HTTP_STATUS` above
    # rather than the registration order of six near-identical decorators.
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
