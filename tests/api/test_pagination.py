"""Every collection can be traversed using the same client loop."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, update
from support.control_plane import (
    API_HEADERS,
    control_plane_app,
    seed_edge,
    seed_site,
    serve,
)

from blitzecdn.capabilities.deployments.adapters.tables import DeploymentRow
from blitzecdn.capabilities.releases.adapters.tables import ReleaseRow
from blitzecdn.capabilities.workflows.adapters.tables import WorkflowRow

COLLECTIONS = [
    "/v3/domains",
    "/v3/hosts",
    "/v3/domains/{domain}/records",
    "/v3/domains/{domain}/rules",
    "/v3/edges",
    "/v3/deployments",
    "/v3/deployments/{deployment_id}/targets",
    "/v3/releases",
    "/v3/audit-events",
    "/v3/workflows",
]


@pytest.fixture
def paginated_client(settings, seeded, monkeypatch):
    control, repository = seeded()
    for number in range(3):
        seed_edge(control, name=f"edge-{number}", host=f"192.0.2.{number + 10}")
        seed_site(
            control,
            name=f"rule-{number}",
            record=f"www{number}",
            domain="example.com",
        )
        seed_site(control, domain=f"zone{number}.example", record="www")
        deployment = control.deployments.deploy("tester")
        repository.workflows.create(f"workflow-{number}", "certificate", "tester")
    # Force timestamp ties so page traversal also checks the ID tie-breaker.
    with repository.database.session() as session:
        for row in [DeploymentRow, ReleaseRow, WorkflowRow]:
            session.execute(
                update(row).values(created_at=datetime(2026, 1, 1, tzinfo=UTC))
            )
    serve(monkeypatch, control)
    queries = []

    def capture(_connection, _cursor, statement, parameters, _context, _many):
        queries.append((statement, parameters))

    event.listen(repository.database.engine, "before_cursor_execute", capture)
    with TestClient(control_plane_app(settings)) as client:
        yield client, deployment.id, queries


@pytest.mark.parametrize("path", COLLECTIONS)
def test_collection_traversal_and_boundary_pages(paginated_client, path):
    client, deployment_id, queries = paginated_client
    path = path.format(domain="example.com", deployment_id=deployment_id)
    assert client.get(path).status_code == 401
    full = client.get(path, params={"limit": 500}, headers=API_HEADERS)
    assert full.status_code == 200, full.text
    body = full.json()
    expected = body["items"]
    assert set(body) == {"items", "total", "limit", "offset"}
    assert body["total"] == len(expected) >= 3
    assert body["limit"] == 500
    assert body["offset"] == 0
    if path in {"/v3/deployments", "/v3/releases", "/v3/workflows"}:
        ids = [item["id"] for item in expected]
        assert ids == sorted(ids, reverse=True)
    if path == "/v3/releases":
        assert all("explanations" not in item for item in expected)
        for detail_path in [
            f"/v3/releases/{expected[0]['id']}",
            "/v3/releases/current",
        ]:
            detail = client.get(detail_path, headers=API_HEADERS)
            assert detail.status_code == 200, detail.text
            assert detail.json()["explanations"]
            assert not {"targets", "sites"} & detail.json().keys()
    if path.endswith("/targets"):
        assert all(not {"deployment_id", "fence"} & item.keys() for item in expected)

    queries.clear()
    seen = []
    for offset in range(0, len(expected), 2):
        response = client.get(
            path, params={"limit": 2, "offset": offset}, headers=API_HEADERS
        )
        assert response.status_code == 200, response.text
        page = response.json()
        assert page["total"] == len(expected)
        assert page["limit"] == 2
        assert page["offset"] == offset
        assert len(page["items"]) <= 2
        seen.extend(page["items"])
    assert seen == expected

    table = {
        "/v3/deployments": "deployments",
        "/v3/releases": "releases",
        "/v3/audit-events": "audit_events",
        "/v3/workflows": "workflows",
        f"/v3/deployments/{deployment_id}/targets": "deployment_targets",
    }.get(path)
    if table:
        reads = [
            (statement, parameters)
            for statement, parameters in queries
            if statement.startswith("SELECT ")
            and not statement.startswith("SELECT count(")
            and f"FROM {table}" in statement
        ]
        assert len(reads) == len(range(0, len(expected), 2))
        assert all(
            "LIMIT" in statement and "OFFSET" in statement for statement, _ in reads
        )
        assert [parameters[-2:] for _, parameters in reads] == [
            (2, offset) for offset in range(0, len(expected), 2)
        ]

    for offset in [len(expected), len(expected) + 10]:
        response = client.get(
            path, params={"limit": 2, "offset": offset}, headers=API_HEADERS
        )
        assert response.status_code == 200
        assert response.json() == {
            "items": [],
            "total": len(expected),
            "limit": 2,
            "offset": offset,
        }


@pytest.mark.parametrize("path", [*COLLECTIONS, "/v3/certificates"])
@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=501",
        "limit=no",
        "offset=-1",
        "offset=no",
        "offset=9223372036854775808",
    ],
)
def test_invalid_pagination_is_rejected(settings, path, query):
    with TestClient(control_plane_app(settings)) as client:
        response = client.get(
            f"{path.format(domain='example.com', deployment_id='missing')}?{query}",
            headers=API_HEADERS,
        )
        assert response.status_code == 422, response.text


def test_all_collections_publish_one_pagination_contract(settings):
    schema = control_plane_app(settings).openapi()
    components = schema["components"]["schemas"]
    for path in [*COLLECTIONS, "/v3/certificates"]:
        operation = schema["paths"][path]["get"]
        parameters = {p["name"]: p for p in operation["parameters"]}
        for name, default, minimum in [("limit", 100, 1), ("offset", 0, 0)]:
            parameter = parameters[name]
            assert parameter["in"] == "query"
            assert parameter["required"] is False
            assert parameter["schema"]["default"] == default
            assert parameter["schema"]["minimum"] == minimum
        assert parameters["limit"]["schema"]["maximum"] == 500
        assert parameters["offset"]["schema"]["maximum"] == 2**63 - 1
        shape = operation["responses"]["200"]["content"]["application/json"]["schema"]
        page = components[shape["$ref"].rsplit("/", 1)[-1]]
        assert set(page["properties"]) == {"items", "total", "limit", "offset"}
        assert set(page["required"]) == {"items", "total", "limit", "offset"}
    assert not any(
        methods.get("get", {})
        .get("responses", {})
        .get("200", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema", {})
        .get("type")
        == "array"
        for methods in schema["paths"].values()
    )


def test_default_limit_and_exact_last_page(settings):
    with TestClient(control_plane_app(settings)) as client:
        for number in range(101):
            created = client.post(
                "/v3/domains",
                json={"name": f"zone{number:03}.example"},
                headers=API_HEADERS,
            )
            assert created.status_code == 201
        first = client.get("/v3/domains", headers=API_HEADERS)
        assert len(first.json()["items"]) == 100
        assert first.json()["total"] == 101
        last = client.get("/v3/domains?limit=2&offset=99", headers=API_HEADERS)
        assert len(last.json()["items"]) == 2
        assert last.json()["total"] == 101


def test_targets_of_a_missing_deployment_return_not_found(settings):
    with TestClient(control_plane_app(settings)) as client:
        response = client.get("/v3/deployments/missing/targets", headers=API_HEADERS)
        assert response.status_code == 404


def test_certificate_filter_is_applied_before_pagination(settings, certificate_pair):
    with TestClient(control_plane_app(settings)) as client:
        for number, days in enumerate([10, 20, 40]):
            domain = f"cert{number}.example"
            assert (
                client.post(
                    "/v3/domains", json={"name": domain}, headers=API_HEADERS
                ).status_code
                == 201
            )
            assert (
                client.post(
                    f"/v3/domains/{domain}/records",
                    json={"name": "cdn", "value": "192.0.2.10"},
                    headers=API_HEADERS,
                ).status_code
                == 201
            )
            certificate, key = certificate_pair(domains=(f"cdn.{domain}",), days=days)
            uploaded = client.post(
                f"/v3/hosts/cert{number}-example/certificate/upload",
                files={
                    "certificate": ("cert.pem", certificate),
                    "private_key": ("key.pem", key),
                },
                headers=API_HEADERS,
            )
            assert uploaded.status_code == 200, uploaded.text
        first = client.get(
            "/v3/certificates?expiring_in=30&limit=1", headers=API_HEADERS
        )
        assert first.status_code == 200
        assert [item["site"] for item in first.json()["items"]] == ["cert0-example"]
        assert first.json()["total"] == 2
        second = client.get(
            "/v3/certificates?expiring_in=30&limit=1&offset=1", headers=API_HEADERS
        )
        assert [item["site"] for item in second.json()["items"]] == ["cert1-example"]
        assert second.json()["total"] == 2
