import json
import re

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from paths import SOURCE

from blitzecdn import __version__
from blitzecdn.api import create_app
from blitzecdn.api.dependencies import get_control_plane
from blitzecdn.features.cache.api import v1 as v1_cache
from blitzecdn.features.cache.api import v2 as v2_cache
from blitzecdn.features.deployments.api import v1 as v1_deployments
from blitzecdn.features.deployments.api import v2 as v2_deployments
from blitzecdn.features.diagnostics.api import readiness as diagnostics
from blitzecdn.features.diagnostics.api import v1 as v1_diagnostics
from blitzecdn.features.diagnostics.api import v2 as v2_diagnostics
from blitzecdn.features.dns.api import v1 as v1_zones
from blitzecdn.features.dns.api import v1_sites, v2_sites
from blitzecdn.features.dns.api import v2 as v2_zones
from blitzecdn.features.edges.api import v1 as v1_edges
from blitzecdn.features.edges.api import v2 as v2_edges
from blitzecdn.features.tls.certificates.api import v1 as v1_certificates
from blitzecdn.features.tls.certificates.api import v2 as v2_certificates


def test_routes_are_domain_modules_and_control_plane_is_a_dependency():
    routers = (
        diagnostics,
        v1_sites,
        v1_zones,
        v1_edges,
        v1_cache,
        v1_certificates,
        v1_deployments,
        v1_diagnostics,
        v2_sites,
        v2_zones,
        v2_edges,
        v2_cache,
        v2_certificates,
        v2_deployments,
        v2_diagnostics,
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
        if route.path in {"/health", "/metrics"}
        or route.path.startswith(("/v1/", "/v2/"))
    }
    assert modules == {
        "blitzecdn.features.diagnostics.api.readiness",
        "blitzecdn.features.cache.api.v1",
        "blitzecdn.features.tls.certificates.api.v1",
        "blitzecdn.features.deployments.api.v1",
        "blitzecdn.features.diagnostics.api.v1",
        "blitzecdn.features.edges.api.v1",
        "blitzecdn.features.dns.api.v1_sites",
        "blitzecdn.features.dns.api.v1",
        "blitzecdn.features.cache.api.v2",
        "blitzecdn.features.tls.certificates.api.v2",
        "blitzecdn.features.deployments.api.v2",
        "blitzecdn.features.diagnostics.api.v2",
        "blitzecdn.features.edges.api.v2",
        "blitzecdn.features.dns.api.v2_sites",
        "blitzecdn.features.dns.api.v2",
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
        if route.path in {"/health", "/metrics"} or route.path.startswith(
            ("/v1/", "/v2/")
        ):
            assert get_control_plane in dependency_calls(route), route.path


def test_openapi_documents_control_and_certificate_workflows(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/docs").status_code == 200
        assert client.get("/redoc").status_code == 200
        schema = client.get("/openapi.json").json()
        assert schema["info"]["version"] == __version__
        paths = schema["paths"]
        assert "/v1/sites" in paths
        assert "/v1/deployments" in paths
        assert "/v1/sites/{name}/certificate/request" in paths
        assert "/v1/sites/{name}/certificate/upload" in paths
        assert "/v1/workflows" in paths
        assert "/v2/sites" in paths
        assert "/v2/deployments" in paths
        assert "/v2/sites/{name}/certificate/request" in paths
        assert "/v2/sites/{name}/certificate/upload" in paths
        assert "/v2/workflows" in paths


def test_v1_is_preserved_while_v2_is_available(settings):
    headers = {"X-API-Key": "x" * 32}
    with TestClient(create_app(settings)) as client:
        v1 = client.get("/v1/sites", headers=headers)
        v2 = client.get("/v2/sites", headers=headers)

    assert v1.status_code == 200
    assert v2.status_code == 200
    assert v1.json() == v2.json() == []


def test_v2_exposes_compression_and_v1_does_not(settings):
    headers = {"X-API-Key": "x" * 32}
    payload = {
        "domain": "example.com",
        "name": "cdn",
        "value": "203.0.113.10",
        "proxied": True,
    }

    with TestClient(create_app(settings)) as client:
        client.post("/v2/domains", json={"name": "example.com"}, headers=headers)
        created = client.post(
            "/v2/domains/example.com/records", json=payload, headers=headers
        )
        assert created.status_code == 201
        assert created.json()["compression"] == "brotli"

        # The frozen version keeps serving the same record without the field.
        v1 = client.get("/v1/sites", headers=headers).json()
        assert "compression" not in v1[0]

        patched = client.patch(
            "/v2/domains/example.com/records/cdn",
            json={"compression": "off"},
            headers=headers,
        )
        assert patched.status_code == 200
        assert patched.json()["compression"] == "off"
        assert (
            client.get("/v2/sites", headers=headers).json()[0]["compression"] == "off"
        )

        rejected = client.patch(
            "/v2/domains/example.com/records/cdn",
            json={"compression": "deflate"},
            headers=headers,
        )
        assert rejected.status_code == 422

        # A v1 PATCH cannot name the field, so it leaves it alone rather than
        # resetting it to the domain default.
        client.patch(
            "/v1/domains/example.com/records/cdn",
            json={"cache_enabled": False},
            headers=headers,
        )
        assert (
            client.get("/v2/sites", headers=headers).json()[0]["compression"] == "off"
        )


def test_v2_exposes_visitor_headers_and_v1_does_not(settings):
    headers = {"X-API-Key": "x" * 32}
    payload = {
        "domain": "example.com",
        "name": "cdn",
        "value": "203.0.113.10",
        "proxied": True,
    }

    with TestClient(create_app(settings)) as client:
        client.post("/v2/domains", json={"name": "example.com"}, headers=headers)
        created = client.post(
            "/v2/domains/example.com/records", json=payload, headers=headers
        )
        assert created.status_code == 201
        assert created.json()["visitor_headers"] == {
            "connecting_ip": True,
            "ip_country": False,
        }

        # The frozen version keeps serving the same record without the block.
        assert (
            "visitor_headers" not in client.get("/v1/sites", headers=headers).json()[0]
        )

        patched = client.patch(
            "/v2/domains/example.com/records/cdn",
            json={"visitor_headers": {"connecting_ip": False, "ip_country": True}},
            headers=headers,
        )
        assert patched.status_code == 200
        assert patched.json()["visitor_headers"] == {
            "connecting_ip": False,
            "ip_country": True,
        }
        site = client.get("/v2/sites", headers=headers).json()[0]
        assert site["visitor_headers"] == {"connecting_ip": False, "ip_country": True}

        # A partial block replaces the whole thing rather than merging, so the
        # unnamed switch comes back at its default.
        replaced = client.patch(
            "/v2/domains/example.com/records/cdn",
            json={"visitor_headers": {"ip_country": True}},
            headers=headers,
        )
        assert replaced.json()["visitor_headers"] == {
            "connecting_ip": True,
            "ip_country": True,
        }

        # No aliases, and no Cloudflare spelling smuggled in as an extra.
        for unknown in ({"cf_connecting_ip": True}, {"true_client_ip": True}):
            rejected = client.patch(
                "/v2/domains/example.com/records/cdn",
                json={"visitor_headers": unknown},
                headers=headers,
            )
            assert rejected.status_code == 422

        # A v1 PATCH cannot name the block, so it leaves it alone rather than
        # resetting it to the domain default.
        client.patch(
            "/v1/domains/example.com/records/cdn",
            json={"cache_enabled": False},
            headers=headers,
        )
        assert client.get("/v2/sites", headers=headers).json()[0][
            "visitor_headers"
        ] == {"connecting_ip": True, "ip_country": True}


def test_openapi_schema_names_are_stable_across_versions(settings):
    """A published component name is part of the contract, not an artefact.

    FastAPI names a component after the class, and pydantic disambiguates a
    collision by qualifying *both* sides with their module path. Two versions
    of one model therefore share a component only while they are structurally
    identical, and the first field v2 gains renames v1's schema too —
    `CdnSite` would become `blitzecdn__api__v1_models__CdnSite`, breaking every
    generated v1 client over a change v1 never made.

    So a v2 representation that diverges takes a version-qualified class name.
    This fails when the next one does not.

    Scoped to the representation models, which are the ones that version. The
    operational schemas used to be published module-qualified for the opposite
    reason — two identical copies of one contract, one per version — and are a
    single shared definition now; `blitzecdn.api.operations` explains how a
    version diverges from those without renaming the other's schema.
    """
    with TestClient(create_app(settings)) as client:
        schemas = client.get("/openapi.json").json()["components"]["schemas"]

    mangled = sorted(
        name
        for name in schemas
        if "api__v1_models__" in name or "api__v2_models__" in name
    )
    assert not mangled, (
        f"{mangled} leak Python module paths into the published API. Give the "
        "diverged v2 class a V2 suffix rather than letting pydantic "
        "disambiguate it — doing that also renames the v1 schema."
    )
    # The names v1 published must still mean v1.
    for name in ("CdnSite", "DnsRecord", "RecordPatch"):
        assert name in schemas, f"v1 lost its published {name} schema"
        assert f"{name}V2" in schemas


def _schemas(settings) -> dict[str, dict]:
    with TestClient(create_app(settings)) as client:
        return client.get("/openapi.json").json()["components"]["schemas"]


def test_published_document_has_no_dangling_schema_references(settings):
    """Every `$ref` resolves. A generated client is only as good as this.

    It did not hold. `HostRun` pointed at `...TaskResult-Input__1`, a component
    the document never defined, so any validator or client generator run over
    it failed on a model at the centre of every deployment response. See the
    `separate_input_output_schemas` comment in `api/app.py` for how a duplicate
    schema pydantic later collapsed left the references behind.
    """
    with TestClient(create_app(settings)) as client:
        document = client.get("/openapi.json").json()
    defined = set(document["components"]["schemas"])
    referenced = set(
        re.findall(r'"#/components/schemas/([^"]+)"', json.dumps(document))
    )
    assert referenced - defined == set()


def test_no_published_component_leaks_a_python_module_path(settings):
    """Not scoped to the representation models any more, because it can be.

    `*_operations.py` used to publish eight schemas per version under names
    like `blitzecdn__api__v1_operations__Deployment`: two identical classes,
    one per version, that pydantic had to disambiguate by module path. They are
    one class now, so the whole document can be held to the rule the
    representation models were already held to.
    """
    leaked = sorted(
        name for name in _schemas(settings) if name.startswith("blitzecdn__")
    )
    assert leaked == []


def test_operational_routes_publish_one_shape_for_both_versions(settings):
    """The v1 and v2 halves of an operational route are the same contract.

    Sharing one definition is what makes this true by construction rather than
    by two files being kept in step by hand. The test is still worth having: it
    is what fails if a version is diverged by editing the shared module, which
    would change *both* versions rather than one.
    """
    schemas = _schemas(settings)

    def resolve(node: object, seen: frozenset[str]) -> object:
        if isinstance(node, dict):
            reference = node.get("$ref")
            if isinstance(reference, str) and set(node) == {"$ref"}:
                name = reference.rsplit("/", 1)[1]
                if name in seen:
                    return {"$recursive": name}
                return resolve(schemas[name], seen | {name})
            return {key: resolve(value, seen) for key, value in node.items()}
        if isinstance(node, list):
            return [resolve(item, seen) for item in node]
        return node

    with TestClient(create_app(settings)) as client:
        paths = client.get("/openapi.json").json()["paths"]

    #: Where v2 deliberately says something v1 cannot: the resource
    #: representations, and nothing else. A route joining this set means a
    #: version has diverged, which is a decision rather than a detail.
    diverged = {
        "/dns/export",
        "/domains/{domain}/records",
        "/domains/{domain}/records/{name}",
        "/sites",
        "/sites/{name}",
    }

    compared = 0
    for path in sorted(paths):
        if not path.startswith("/v1/"):
            continue
        suffix = path.removeprefix("/v1")
        if suffix in diverged or f"/v2{suffix}" not in paths:
            continue
        v1 = resolve(paths[path], frozenset())
        v2 = resolve(paths[f"/v2{suffix}"], frozenset())
        assert json.dumps(_unversioned(v1), sort_keys=True) == json.dumps(
            _unversioned(v2), sort_keys=True
        ), suffix
        compared += 1
    assert compared == 23, "the operational surface changed; check the route table"


def _unversioned(node: object) -> object:
    """Neutralise the parts of an operation that name its own version.

    The path prefix, and the operation ids and titles FastAPI derives from the
    endpoint function's name. What is left is the contract.
    """
    if isinstance(node, dict):
        return {key: _unversioned(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_unversioned(item) for item in node]
    if isinstance(node, str):
        return re.sub(
            r"(?i)\bv[12]\b", "vN", node.replace("_v1_", "_vN_").replace("_v2_", "_vN_")
        )
    return node


#: The operational contract v1 froze, as field names and required fields. The
#: shapes live in one module now and both versions serve them, so this is what
#: stands between "v2 needs another field on a deployment" and that field
#: silently appearing on v1 as well. A version that has to diverge declares its
#: own class instead — see the module docstring of `blitzecdn.api.operations`.
FROZEN_V1_OPERATIONS = {
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
    "PurgeResult": (
        "complete entries failed_hosts host_limit hosts purge_all purged_at",
        "complete failed_hosts purged_at",
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


def test_frozen_v1_operational_shapes_are_unchanged(settings):
    schemas = _schemas(settings)
    actual = {
        name: (
            " ".join(sorted(schemas[name]["properties"])),
            " ".join(sorted(schemas[name].get("required", ()))),
        )
        for name in FROZEN_V1_OPERATIONS
        if name in schemas
    }
    assert actual == FROZEN_V1_OPERATIONS


def test_neither_version_imports_the_other():
    """A shared module is fine. A shared module is not v1 importing v2."""
    root = SOURCE
    offenders = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        own = "v2" if "v2" in path.name else "v1" if "v1" in path.name else None
        if own is None:
            continue
        other = "v1" if own == "v2" else "v2"
        offenders.extend(
            f"{path.relative_to(root)} references {module}"
            for module in (f"blitzecdn.api.{other}_models", f"blitzecdn.api.{other}_")
            if module in text
        )
    assert offenders == []


def test_openapi_generation_is_deterministic(settings):
    """The same source must publish byte-identical documents.

    `docs-check` compares the reference pages against a freshly generated
    document, and client generators diff one build against the last. Both treat
    a reordered `components` block or a renamed disambiguated schema as a real
    change. Schema generation walks sets and dicts of models, so a document
    that shuffles between builds would turn either into noise that hides the
    one change that mattered.
    """
    first = json.dumps(_schemas(settings), sort_keys=False)
    second = json.dumps(_schemas(settings), sort_keys=False)
    assert first == second
