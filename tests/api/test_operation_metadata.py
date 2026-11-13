"""Every published operation is grouped, named and typed for generated clients.

SDK generators turn `operationId` into method names and `tags` into modules, so
both are set by hand on each route rather than derived from its path, and a
success body is a model a generator can name.
"""

import re

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from support.control_plane import configured_settings, control_plane_app

from blitzecdn.composition import BUILTIN_REGISTRY

#: The one tag per area, as `docs/api-defaults.md` divides the paths.
TAGS = {
    "audit",
    "cache",
    "certificates",
    "deployments",
    "domains",
    "edges",
    "operations",
    "origins",
    "releases",
    "sites",
    "status",
    "traffic",
    "workflows",
}

#: Successes with no JSON model, and why.
UNTYPED_SUCCESS = {
    # Prometheus text exposition.
    ("get", "/metrics"),
}


@pytest.fixture(scope="module")
def app(tmp_path_factory: pytest.TempPathFactory) -> FastAPI:
    return control_plane_app(configured_settings(tmp_path_factory.mktemp("ops")))


def _operations(app: FastAPI):
    for path, operations in app.openapi()["paths"].items():
        for method, operation in operations.items():
            yield method, path, operation


def test_every_route_names_its_operation_explicitly():
    """An id FastAPI derives from the path changes whenever the path does."""
    unnamed = [
        route.path
        for router in BUILTIN_REGISTRY.api_routers()
        for route in router.routes
        if isinstance(route, APIRoute)
        and route.include_in_schema
        and route.operation_id is None
    ]
    assert unnamed == []


def test_operation_ids_are_unique_and_lead_with_their_tag(app):
    ids = [operation["operationId"] for _, _, operation in _operations(app)]
    assert len(ids) == len(set(ids))
    wrong = [
        operation["operationId"]
        for _, _, operation in _operations(app)
        if not re.fullmatch(r"[a-z]+(_[a-z]+)+", operation["operationId"])
        or not operation["operationId"].startswith(f"{operation['tags'][0]}_")
    ]
    assert wrong == []


def test_every_operation_has_exactly_one_known_tag(app):
    tags = {
        f"{method.upper()} {path}": operation.get("tags")
        for method, path, operation in _operations(app)
    }
    assert {name: tag for name, tag in tags.items() if len(tag or []) != 1} == {}
    assert {tag[0] for tag in tags.values()} == TAGS


def test_every_success_body_is_a_named_model(app):
    untyped = []
    for method, path, operation in _operations(app):
        if (method, path) in UNTYPED_SUCCESS:
            continue
        for code, response in operation["responses"].items():
            if not code.startswith("2") or code == "204":
                continue
            schema = (
                response.get("content", {}).get("application/json", {}).get("schema")
            )
            if not schema or "$ref" not in schema:
                untyped.append(f"{method.upper()} {path} {code}")
    assert untyped == []


def test_certificate_expiry_window_says_its_unit(app):
    (parameter,) = [
        parameter
        for parameter in app.openapi()["paths"]["/v3/certificates"]["get"]["parameters"]
        if parameter["name"] == "expiring_in"
    ]
    assert "days" in parameter["description"]
