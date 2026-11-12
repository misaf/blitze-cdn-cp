"""Each operation publishes the error responses its route can produce.

The rules read the route itself — its dependencies, path, body and method — so
an endpoint added without its declarations fails here, not in a generated
client that never expected a 404.
"""

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from support.control_plane import configured_settings, control_plane_app

from blitzecdn.composition import BUILTIN_REGISTRY
from blitzecdn.delivery.api.dependencies import require_operator

ERROR_SCHEMA = {"$ref": "#/components/schemas/ErrorResponse"}


def _calls(dependant: Dependant) -> Iterator[object]:
    for dependency in dependant.dependencies:
        yield dependency.call
        yield from _calls(dependency)


def _required(route: APIRoute) -> set[str]:
    """The error statuses ``route`` must declare, from its shape alone."""
    # Every operation sits behind the IP gate, can fail unexpectedly, and
    # validates its request.
    codes = {"403", "422", "500"}
    if require_operator in set(_calls(route.dependant)):
        codes |= {"401", "503"}
    if route.dependant.path_params:
        codes.add("404")
    if route.body_field is not None:
        codes |= {"400", "413"}
    if route.methods - {"GET", "HEAD"}:
        codes.add("409")
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
        and response.get("content") != {"application/json": {"schema": ERROR_SCHEMA}}
    ]
    assert wrong == []


def test_only_an_operation_that_takes_the_deployment_lock_advertises_retry_after(
    app,
):
    """The busy 409 is the one a client should retry; the others will not clear."""
    retrying = {
        f"{method.upper()} {path}"
        for path, operations in app.openapi()["paths"].items()
        for method, operation in operations.items()
        if "Retry-After" in operation["responses"].get("409", {}).get("headers", {})
    }
    assert retrying == {"POST /v3/ssl/automatic/reconcile"}


def test_synchronous_edge_work_declares_a_gateway_error(app):
    gateway = {
        f"{method.upper()} {path}"
        for path, operations in app.openapi()["paths"].items()
        for method, operation in operations.items()
        if "502" in operation["responses"]
    }
    assert gateway == {"POST /v3/ssl/automatic/reconcile"}
