from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from blitzecdn import __version__
from blitzecdn.api import create_app
from blitzecdn.api.dependencies import get_control_plane
from blitzecdn.features.cache.api import v1 as v1_cache
from blitzecdn.features.cache.api import v2 as v2_cache
from blitzecdn.features.certificates.api import v1 as v1_certificates
from blitzecdn.features.certificates.api import v2 as v2_certificates
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
        "blitzecdn.features.certificates.api.v1",
        "blitzecdn.features.deployments.api.v1",
        "blitzecdn.features.diagnostics.api.v1",
        "blitzecdn.features.edges.api.v1",
        "blitzecdn.features.dns.api.v1_sites",
        "blitzecdn.features.dns.api.v1",
        "blitzecdn.features.cache.api.v2",
        "blitzecdn.features.certificates.api.v2",
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

    Scoped to the representation models on purpose: several of the operational
    schemas in `*_operations.py` are already published module-qualified, which
    is its own cleanup and not one to fold into an unrelated change.
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
