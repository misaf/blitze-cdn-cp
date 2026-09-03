import json
import re

from control_plane_fixtures import control_plane_app
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from blitzecdn import __version__
from blitzecdn.api.dependencies import get_control_plane
from blitzecdn.capabilities.deployments.api import routes as deployment_routes
from blitzecdn.capabilities.diagnostics.api import readiness as diagnostics
from blitzecdn.capabilities.diagnostics.api import routes as diagnostic_routes
from blitzecdn.capabilities.dns.api import routes as zone_routes
from blitzecdn.capabilities.edges.api import routes as edge_routes
from blitzecdn.capabilities.sites.api import routes as site_routes

REQUIRES_CERTIFICATES = frozenset({"test_published_operational_shapes_are_pinned"})


def test_routes_are_domain_modules_and_control_plane_is_a_dependency():
    """Every router this *distribution* ships, and where its endpoints live.

    Scoped to the control plane's own routers on purpose. An optional
    distribution's routes are its own to hold — `packages/blitzecdn-cache`
    asserts the identical properties over `/v1/cache/*` — and a core test that
    named them would fail the moment the package it was asserting about was
    detached, which is the supported operation this whole boundary exists for.
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
        "blitzecdn.capabilities.sites.api.routes",
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
            assert get_control_plane in dependency_calls(route), route.path


def test_openapi_documents_core_control_workflows(settings):
    with TestClient(control_plane_app(settings)) as client:
        assert client.get("/docs").status_code == 200
        assert client.get("/redoc").status_code == 200
        schema = client.get("/openapi.json").json()
        assert schema["info"]["version"] == __version__
        paths = schema["paths"]
        assert "/v1/sites" in paths
        assert "/v1/deployments" in paths
        assert "/v1/workflows" in paths
        assert not [path for path in paths if path.startswith("/v2/")]


def _schemas(settings) -> dict[str, dict]:
    with TestClient(control_plane_app(settings)) as client:
        return client.get("/openapi.json").json()["components"]["schemas"]


def test_published_document_has_no_dangling_schema_references(settings):
    """Every `$ref` resolves. A generated client is only as good as this.

    It did not hold. `HostRun` pointed at `...TaskResult-Input__1`, a component
    the document never defined, so any validator or client generator run over
    it failed on a model at the centre of every deployment response. See the
    `separate_input_output_schemas` comment in `api/app.py` for how a duplicate
    schema pydantic later collapsed left the references behind.
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

    Pydantic disambiguates two same-named classes by qualifying *both* with
    their module path, so a second `Deployment` anywhere would publish
    `blitzecdn__api__operations__Deployment` and rename the original as a side
    effect. One class per published name is what keeps that from happening.
    """
    leaked = sorted(
        name for name in _schemas(settings) if name.startswith("blitzecdn__")
    )
    assert leaked == []


#: The operational shapes the API publishes, as field names and required
#: fields. Pinned rather than derived: these are what a generated client binds
#: to, and `blitzecdn.api.operations` is shared by every route that reports an
#: operation, so an edit there reaches all of them at once. Changing this table
#: is how such a change is declared.
PUBLISHED_OPERATION_SHAPES = {
    "AnsibleRun": (
        "error finished_at hosts id log_path playbook return_code started_at "
        "status targeted",
        "finished_at id playbook started_at status",
    ),
    "AuditEvent": (
        "action created_at details id operator resource_id resource_type",
        "action created_at id operator resource_type",
    ),
    "Deployment": (
        "canonical_digest check_mode created_at finished_at host_limit id "
        "operator result rollback_of started_at status",
        "check_mode created_at id operator status",
    ),
    "DriftReport": (
        "checked_at deployment_id host_limit hosts unattempted",
        "checked_at deployment_id",
    ),
    "EdgeRemoval": ("decommissioned hosts name", "decommissioned name"),
    "HostRun": (
        "changed changes failed failures host ignored ok report rescued "
        "skipped unreachable",
        "host",
    ),
    "ReconciliationResult": ("deployment failed issued skipped", ""),
    "SslAutomaticReconciliation": (
        "deployment scanned skipped upgraded",
        "",
    ),
    "TaskResult": ("action message outcome role task", "outcome task"),
    "Workflow": (
        "created_at error id kind operator resource_id status steps updated_at",
        "created_at id kind operator status updated_at",
    ),
}


def test_published_operational_shapes_are_pinned(settings):
    schemas = _schemas(settings)
    actual = {
        name: (
            " ".join(sorted(schemas[name]["properties"])),
            " ".join(sorted(schemas[name].get("required", ()))),
        )
        for name in PUBLISHED_OPERATION_SHAPES
        if name in schemas
    }
    assert actual == PUBLISHED_OPERATION_SHAPES


def test_openapi_generation_is_deterministic(settings):
    """The same source must publish byte-identical documents.

    Client generators diff one build against the last, and treat a reordered
    `components` block or a renamed disambiguated schema as a real change.
    Schema generation walks sets and dicts of models, so a document that
    shuffles between builds would turn that into noise hiding the one change
    that mattered.
    """
    first = json.dumps(_schemas(settings), sort_keys=False)
    second = json.dumps(_schemas(settings), sort_keys=False)
    assert first == second
