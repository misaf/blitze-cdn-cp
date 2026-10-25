import json
import re

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from support.control_plane import control_plane_app

from blitzecdn import __version__
from blitzecdn.capabilities.deployments.api import routes as deployment_routes
from blitzecdn.capabilities.diagnostics.api import readiness as diagnostics
from blitzecdn.capabilities.diagnostics.api import routes as diagnostic_routes
from blitzecdn.capabilities.dns.api import routes as site_routes
from blitzecdn.capabilities.dns.api import routes as zone_routes
from blitzecdn.capabilities.edges.api import routes as edge_routes
from blitzecdn.entrypoints.api.dependencies import get_services


def test_routes_are_domain_modules_and_services_are_a_dependency():
    """Every router the control plane itself ships, and where its endpoints live.

    Capability routers are asserted in their own suites.
    """
    routers = (
        diagnostics,
        site_routes,
        zone_routes,
        edge_routes,
        deployment_routes,
        diagnostic_routes,
    )
    routes = [
        route
        for module in routers
        for route in module.router.routes
        if isinstance(route, APIRoute)
    ]
    modules = {
        route.endpoint.__module__
        for route in routes
        if route.path in {"/health", "/metrics"} or route.path.startswith("/v1/")
    }
    assert modules == {
        "blitzecdn.capabilities.diagnostics.api.readiness",
        "blitzecdn.capabilities.deployments.api.routes",
        "blitzecdn.capabilities.diagnostics.api.routes",
        "blitzecdn.capabilities.edges.api.routes",
        "blitzecdn.capabilities.dns.api.routes",
    }

    def dependency_calls(route: APIRoute) -> set[object]:
        pending = list(route.dependant.dependencies)
        calls: set[object] = set()
        while pending:
            dependency = pending.pop()
            calls.add(dependency.call)
            pending.extend(dependency.dependencies)
        return calls

    for route in routes:
        if route.path in {"/health", "/metrics"} or route.path.startswith("/v1/"):
            assert get_services in dependency_calls(route), route.path


def test_openapi_documents_core_control_workflows(settings):
    with TestClient(control_plane_app(settings)) as client:
        assert client.get("/docs").status_code == 200
        assert client.get("/redoc").status_code == 200
        schema = client.get("/openapi.json").json()
        assert schema["info"]["version"] == __version__
        paths = schema["paths"]
        assert "/v1/hosts" in paths
        assert "/v1/deployments" in paths
        assert "/v1/workflows" in paths
        assert not [path for path in paths if path.startswith("/v2/")]


def _schemas(settings) -> dict[str, dict]:
    with TestClient(control_plane_app(settings)) as client:
        return client.get("/openapi.json").json()["components"]["schemas"]


def test_published_document_has_no_dangling_schema_references(settings):
    """Every `$ref` resolves. A generated client is only as good as this.

    See the `separate_input_output_schemas` comment in `api/app.py`.
    """
    with TestClient(control_plane_app(settings)) as client:
        document = client.get("/openapi.json").json()
    defined = set(document["components"]["schemas"])
    referenced = set(
        re.findall(r'"#/components/schemas/([^"]+)"', json.dumps(document))
    )
    assert referenced - defined == set()


def test_no_published_component_leaks_a_python_module_path(settings):
    """A component is named after its class, and that name is the contract.

    Pydantic qualifies both same-named classes with their module paths, so a
    second `Deployment` would rename the first. One class per published name.
    """
    leaked = sorted(
        name for name in _schemas(settings) if name.startswith("blitzecdn__")
    )
    assert leaked == []


def test_openapi_generation_is_deterministic(settings):
    """The same source must publish byte-identical documents.

    Client generators diff builds, and schema generation walks sets and dicts, so
    nondeterminism would be noise hiding real changes.
    """
    first = json.dumps(_schemas(settings), sort_keys=False)
    second = json.dumps(_schemas(settings), sort_keys=False)
    assert first == second
