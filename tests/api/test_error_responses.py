"""Each operation publishes the error responses its route can produce.

The rules read the route itself — its dependencies, path, body and method — so
an endpoint added without its declarations fails here, not in a generated
client that never expected a 404.
"""

import re
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_flat_params
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from support.control_plane import API_HEADERS, configured_settings, control_plane_app

from blitzecdn.capabilities.cache.service import CacheService
from blitzecdn.capabilities.deployments import DeploymentService
from blitzecdn.capabilities.tls.certificates.service import CertificateService
from blitzecdn.composition import BUILTIN_REGISTRY
from blitzecdn.core.exceptions import DeploymentBusyError, NotFoundError
from blitzecdn.delivery.api.dependencies import require_operator

ERROR_SCHEMA = {"$ref": "#/components/schemas/ErrorResponse"}


def _calls(dependant: Dependant) -> Iterator[object]:
    for dependency in dependant.dependencies:
        yield dependency.call
        yield from _calls(dependency)


def _required(route: APIRoute) -> set[str]:
    """The error statuses ``route`` must declare, from its shape alone."""
    # Every operation sits behind the IP gate and can fail unexpectedly.
    codes = {"403", "500"}
    if get_flat_params(route.dependant) or route.body_field is not None:
        codes.add("422")
    if require_operator in set(_calls(route.dependant)):
        codes |= {"401", "503"}
    if route.dependant.path_params:
        codes.add("404")
    if route.body_field is not None:
        codes |= {"400", "413"}
    if route.methods - {"GET", "HEAD"}:
        codes.add("409")
    if any(param.alias == "If-Match" for param in route.dependant.header_params):
        codes.add("412")
    return codes


@pytest.fixture(scope="module")
def app(tmp_path_factory: pytest.TempPathFactory) -> FastAPI:
    return control_plane_app(configured_settings(tmp_path_factory.mktemp("errors")))


def _operations(app: FastAPI) -> Iterator[tuple[APIRoute, str, dict]]:
    """Each published operation, beside the route that serves it."""
    paths = app.openapi()["paths"]
    # The application holds its routers as included wholes; the registry's are
    # the same routers, route by route.
    routes = [
        route
        for router in BUILTIN_REGISTRY.api_routers()
        for route in router.routes
        if isinstance(route, APIRoute) and route.include_in_schema
    ]
    assert routes
    for route in routes:
        for method in route.methods:
            yield route, method, paths[route.path_format][method.lower()]


def test_every_operation_declares_the_errors_its_route_can_produce(app):
    missing = {
        f"{method} {route.path}": sorted(_required(route) - set(operation["responses"]))
        for route, method, operation in _operations(app)
    }
    assert {name: codes for name, codes in missing.items() if codes} == {}


def test_only_an_operation_that_reads_input_declares_validation(app):
    """Nothing to validate, nothing to reject: no 422 without parameters or body."""
    stray = [
        f"{method} {route.path}"
        for route, method, operation in _operations(app)
        if "422" in operation["responses"] and "422" not in _required(route)
    ]
    assert stray == []
    assert "422" not in app.openapi()["paths"]["/health"]["get"]["responses"]


def test_every_error_but_validation_is_a_detail_message(app):
    schema = app.openapi()
    assert schema["components"]["schemas"]["ErrorResponse"] == {
        "additionalProperties": False,
        "properties": {"detail": {"title": "Detail", "type": "string"}},
        "required": ["detail"],
        "title": "ErrorResponse",
        "type": "object",
    }
    wrong = [
        f"{method.upper()} {path} {code}"
        for path, operations in schema["paths"].items()
        for method, operation in operations.items()
        for code, response in operation["responses"].items()
        if int(code) >= 400
        and code != "422"
        # A failed health check reports which, below.
        and (path, code) != ("/health", "503")
        and response.get("content") != {"application/json": {"schema": ERROR_SCHEMA}}
    ]
    assert wrong == []


def test_a_health_check_failure_is_published_with_its_own_body(app):
    """`/health` answers 503 with the failed check, not an error message."""
    unavailable = app.openapi()["paths"]["/health"]["get"]["responses"]["503"]
    assert unavailable["content"] == {
        "application/json": {
            "schema": {"$ref": "#/components/schemas/HealthUnavailable"}
        }
    }


def _raise(error: Exception):
    def raising(*_args, **_kwargs):
        raise error

    return raising


#: What a route's shape cannot show: a resource named in the body, and a
#: deployment lock taken while the request is open. Each case makes the
#: service fail as it would, and the answer must be one the operation declares.
ANSWERS = [
    pytest.param(
        CacheService,
        "check_purge",
        NotFoundError("hostname is not served: unknown.example"),
        ("POST", "/v3/cache/purge", {"json": {"purge_all": True}}),
        id="purge-of-an-unserved-hostname",
    ),
    pytest.param(
        CertificateService,
        "admit_renewal",
        NotFoundError("no managed certificate for: unknown"),
        ("POST", "/v3/certificates/renew", {"json": {}}),
        id="renewal-of-an-unmanaged-site",
    ),
    pytest.param(
        DeploymentService,
        "submit_deployment",
        DeploymentBusyError("another deployment is already running"),
        ("POST", "/v3/deployments", {"json": {}}),
        id="deploy-while-busy",
    ),
    pytest.param(
        DeploymentService,
        "submit_rollback",
        DeploymentBusyError("another deployment is already running"),
        ("POST", "/v3/deployments/rollback", {"json": {}}),
        id="rollback-while-busy",
    ),
    pytest.param(
        CertificateService,
        "upload_certificate",
        DeploymentBusyError("another deployment is already running"),
        (
            "PUT",
            "/v3/sites/example-com/certificate",
            {"files": {"certificate": b"cert", "private_key": b"key"}},
        ),
        id="certificate-upload-while-busy",
    ),
]


@pytest.mark.parametrize(("service", "method", "error", "request_"), ANSWERS)
def test_answers_a_route_shape_cannot_show_are_declared(
    settings, monkeypatch, service, method, error, request_
):
    verb, path, body = request_
    monkeypatch.setattr(service, method, _raise(error))
    application = control_plane_app(settings)
    with TestClient(application) as client:
        response = client.request(verb, path, headers=API_HEADERS, **body)
    assert response.status_code == (
        409 if isinstance(error, DeploymentBusyError) else 404
    )
    declared = application.openapi()["paths"]
    # A literal path wins over a template that also matches it, as in routing.
    template = (
        path
        if path in declared
        else next(
            candidate
            for candidate in declared
            if re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", candidate), path)
        )
    )
    responses = declared[template][verb.lower()]["responses"]
    assert str(response.status_code) in responses
    for header in response.headers.keys() & {"retry-after"}:
        assert header.title() in responses[str(response.status_code)]["headers"]


def test_only_routes_that_take_the_deployment_lock_advertise_retry_after(app):
    """The busy 409 is the one a client should retry; the others will not clear."""
    retrying = {
        f"{method.upper()} {path}"
        for path, operations in app.openapi()["paths"].items()
        for method, operation in operations.items()
        if "Retry-After" in operation["responses"].get("409", {}).get("headers", {})
    }
    assert retrying == {
        "POST /v3/deployments",
        "POST /v3/deployments/rollback",
        "PUT /v3/sites/{name}/certificate",
    }
