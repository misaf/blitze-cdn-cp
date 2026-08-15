"""FastAPI dependencies shared by the HTTP route modules."""

import hmac
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, cast

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader

from blitzecdn.config import Settings
from blitzecdn.control_plane import ControlPlane


def get_control_plane(request: Request) -> ControlPlane:
    return cast(ControlPlane, request.app.state.control_plane)


def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_renewal_pool(request: Request) -> ThreadPoolExecutor:
    return cast(ThreadPoolExecutor, request.app.state.renewal_pool)


SettingsDependency = Annotated[Settings, Depends(get_settings)]


# Declared as a security scheme rather than a plain header parameter, so the
# generated OpenAPI carries `components.securitySchemes` and every protected
# operation references it. Tooling that reads the schema (Swagger UI, Postman)
# then treats the key as authentication instead of a required header to type in
# by hand. `auto_error=False` keeps the 401 body and `WWW-Authenticate` header
# below as the single source of the rejection.
api_key_scheme = APIKeyHeader(
    name="x-api-key",
    scheme_name="ApiKeyAuth",
    description="Operator API key. Supply the value per environment; it is "
    "never part of this specification.",
    auto_error=False,
)


def require_operator(
    settings: SettingsDependency,
    x_api_key: Annotated[str | None, Depends(api_key_scheme)] = None,
) -> str:
    if not settings.api_keys:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "API authentication is not configured",
        )
    if x_api_key:
        for operator, expected in settings.api_keys.items():
            if hmac.compare_digest(x_api_key, expected.get_secret_value()):
                return operator
    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "invalid or missing API key",
        headers={"WWW-Authenticate": "ApiKey"},
    )


ControlPlaneDependency = Annotated[ControlPlane, Depends(get_control_plane)]
OperatorDependency = Annotated[str, Depends(require_operator)]
RenewalPoolDependency = Annotated[ThreadPoolExecutor, Depends(get_renewal_pool)]
