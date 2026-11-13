import json
import re

from dishka.integrations.fastapi import DishkaSyncRoute
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from support.control_plane import control_plane_app

from blitzecdn import __version__
from blitzecdn.capabilities.deployments.api import routes as deployment_routes
from blitzecdn.capabilities.diagnostics.api import readiness as diagnostics
from blitzecdn.capabilities.diagnostics.api import routes as diagnostic_routes
from blitzecdn.capabilities.edges.api import routes as edge_routes
from blitzecdn.capabilities.sites.api import routes as site_routes
from blitzecdn.capabilities.sites.api import routes as zone_routes
from blitzecdn.composition import BUILTIN_REGISTRY


def test_routes_are_domain_modules():
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
        if route.path in {"/health", "/metrics"} or route.path.startswith("/v3/")
    }
    assert modules == {
        "blitzecdn.capabilities.diagnostics.api.readiness",
        "blitzecdn.capabilities.deployments.api.routes",
        "blitzecdn.capabilities.diagnostics.api.routes",
        "blitzecdn.capabilities.edges.api.routes",
        "blitzecdn.capabilities.sites.api.routes",
    }


def test_every_route_resolves_its_services_from_the_container():
    """A router without dishka's route class would read ``FromDishka[...]``
    as a request body, and publish it."""
    routes = [
        route
        for router in BUILTIN_REGISTRY.api_routers()
        for route in router.routes
        if isinstance(route, APIRoute)
    ]
    assert routes
    assert [
        route.path for route in routes if not isinstance(route, DishkaSyncRoute)
    ] == []


def test_openapi_documents_core_control_workflows(settings):
    with TestClient(control_plane_app(settings)) as client:
        assert client.get("/docs").status_code == 200
        assert client.get("/redoc").status_code == 200
        schema = client.get("/openapi.json").json()
        assert schema["info"]["version"] == __version__
        paths = schema["paths"]
        assert "/v3/sites" in paths
        assert "/v3/deployments" in paths
        assert "/v3/workflows" in paths
        assert all(
            path in {"/health", "/metrics"} or path.startswith("/v3/") for path in paths
        )
        for prefix in ("/v1", "/v2"):
            assert client.get(f"{prefix}/hosts").status_code == 404


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


#: Path segments that address a resource without being its collection's name.
#: `/v3/traffic/status/{edge}` is a report about an edge, so `status` reads as
#: `edges` here, and the edge keeps the one parameter name it has everywhere.
_SEGMENT_RESOURCE = {"status": "edges"}


def _path_parameters(path: str) -> list[tuple[str, str, bool]]:
    """``(collection, parameter, identifies)`` for each parameter in ``path``.

    A parameter belongs to the literal segment before it. In a run of several,
    such as ``records/{type}/{record}``, the last one identifies the resource
    and the rest qualify it.
    """
    found: list[tuple[str, str, bool]] = []
    collection = ""
    segments = path.strip("/").split("/")
    for index, segment in enumerate(segments):
        if not segment.startswith("{"):
            collection = _SEGMENT_RESOURCE.get(segment, segment)
            continue
        following = segments[index + 1] if index + 1 < len(segments) else ""
        found.append((collection, segment[1:-1], not following.startswith("{")))
    return found


def test_a_resource_has_one_path_parameter_name(settings):
    """Generated clients name arguments after path parameters, so one site
    must not be ``name`` in one call and ``site`` in another.

    Each parameter is named after its resource, never a bare ``name`` or ``id``,
    which would be ambiguous in a path holding two, such as a zone's rule.
    """
    with TestClient(control_plane_app(settings)) as client:
        paths = client.get("/openapi.json").json()["paths"]
    collections_by_parameter: dict[str, set[str]] = {}
    identifiers_by_collection: dict[str, set[str]] = {}
    generic: set[str] = set()
    for path in paths:
        for collection, parameter, identifies in _path_parameters(path):
            if parameter in {"name", "id"}:
                generic.add(path)
            collections_by_parameter.setdefault(parameter, set()).add(collection)
            if identifies:
                identifiers_by_collection.setdefault(collection, set()).add(parameter)
    assert sorted(generic) == []
    assert {
        parameter: sorted(collections)
        for parameter, collections in collections_by_parameter.items()
        if len(collections) > 1
    } == {}
    assert {
        collection: sorted(parameters)
        for collection, parameters in identifiers_by_collection.items()
        if len(parameters) > 1
    } == {}
