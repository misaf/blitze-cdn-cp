import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from blitzecdn.application import ControlPlane
from blitzecdn.config import Settings
from blitzecdn.domain.models import AuditEvent, CdnSite, Deployment, SitePatch
from blitzecdn.exceptions import BlitzeError, ConflictError, NotFoundError


class DeployRequest(BaseModel):
    check: bool = False


class RollbackRequest(BaseModel):
    deployment_id: str | None = Field(default=None, min_length=32, max_length=32)
    check: bool = False


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings.from_environment()
    control_plane = ControlPlane(resolved)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        control_plane.initialize()
        yield

    application = FastAPI(
        title="BlitzeCDN control plane",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    def require_operator(x_api_key: str | None = Header(default=None)) -> str:
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

    @application.get("/v1/sites", response_model=list[CdnSite])
    def list_sites(_operator: operator_dependency) -> list[CdnSite]:
        return control_plane.repository.list_sites()

    @application.post(
        "/v1/sites", response_model=CdnSite, status_code=status.HTTP_201_CREATED
    )
    def create_site(site: CdnSite, operator: operator_dependency) -> CdnSite:
        return control_plane.create_site(site, operator)

    @application.patch("/v1/sites/{name}", response_model=CdnSite)
    def update_site(
        name: str, patch: SitePatch, operator: operator_dependency
    ) -> CdnSite:
        return control_plane.update_site(name, patch, operator)

    @application.delete("/v1/sites/{name}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_site(name: str, operator: operator_dependency) -> None:
        control_plane.delete_site(name, operator)

    @application.post("/v1/deployments", response_model=Deployment)
    def deploy(request: DeployRequest, operator: operator_dependency) -> Deployment:
        return control_plane.deploy(operator, check=request.check)

    @application.get("/v1/deployments", response_model=list[Deployment])
    def deployments(
        _operator: operator_dependency, limit: int = Query(20, ge=1, le=100)
    ) -> list[Deployment]:
        return control_plane.repository.list_deployments(limit)

    @application.get("/v1/deployments/{deployment_id}", response_model=Deployment)
    def deployment(deployment_id: str, _operator: operator_dependency) -> Deployment:
        return control_plane.repository.get_deployment(deployment_id)

    @application.post("/v1/rollbacks", response_model=Deployment)
    def rollback(request: RollbackRequest, operator: operator_dependency) -> Deployment:
        return control_plane.rollback(
            operator, request.deployment_id, check=request.check
        )

    @application.get("/v1/audit-events", response_model=list[AuditEvent])
    def audit_events(
        _operator: operator_dependency, limit: int = Query(100, ge=1, le=500)
    ) -> list[AuditEvent]:
        return control_plane.repository.list_audit_events(limit)

    return application


def _error_response(status_code: int, detail: str) -> object:
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=status_code, content={"detail": detail})
