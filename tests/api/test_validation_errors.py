"""The same documented 422 contract before and after request parsing."""

import pytest
from fastapi.testclient import TestClient
from support.control_plane import API_HEADERS, control_plane_app


@pytest.mark.parametrize(
    ("method", "path", "kwargs", "expected"),
    [
        ("POST", "/v3/domains", {"json": {}}, [("missing", ["body", "name"])]),
        (
            "POST",
            "/v3/domains",
            {"json": {"unexpected": "private-value"}},
            [
                ("missing", ["body", "name"]),
                ("extra_forbidden", ["body", "unexpected"]),
            ],
        ),
        (
            "PATCH",
            "/v3/domains/example.com",
            {"json": {"cache_enabled": None}},
            [("value_error", ["body", "cache_enabled"])],
        ),
        (
            "GET",
            "/v3/domains?limit=0",
            {},
            [("greater_than_equal", ["query", "limit"])],
        ),
        (
            "GET",
            "/v3/domains?offset=private-value",
            {},
            [("int_parsing", ["query", "offset"])],
        ),
        (
            "POST",
            "/v3/hosts/example-com/certificate/upload",
            {},
            [
                ("missing", ["body", "certificate"]),
                ("missing", ["body", "private_key"]),
            ],
        ),
    ],
)
def test_request_errors_have_locations_and_no_raw_input(
    settings, method, path, kwargs, expected
):
    with TestClient(control_plane_app(settings)) as client:
        response = client.request(method, path, headers=API_HEADERS, **kwargs)
    assert response.status_code == 422, response.text
    body = response.json()
    assert set(body) == {"detail"}
    assert [(issue["type"], issue["loc"]) for issue in body["detail"]] == expected
    assert all(set(issue) == {"type", "loc", "msg"} for issue in body["detail"])
    assert all(
        isinstance(issue["msg"], str) and issue["msg"] for issue in body["detail"]
    )
    assert "private-value" not in response.text


def test_malformed_json_uses_the_same_error_contract(settings):
    with TestClient(control_plane_app(settings)) as client:
        response = client.post(
            "/v3/domains",
            content='{"name":',
            headers={**API_HEADERS, "Content-Type": "application/json"},
        )
    assert response.status_code == 422
    issue = response.json()["detail"][0]
    assert set(issue) == {"type", "loc", "msg"}
    assert issue["type"] == "json_invalid"
    assert issue["loc"][0] == "body"
    assert isinstance(issue["loc"][1], int)


def test_cross_field_validation_matches_on_create_and_patch_and_rolls_back(settings):
    with TestClient(control_plane_app(settings)) as client:
        invalid_create = client.post(
            "/v3/domains",
            json={"name": "example.com", "http3_enabled": True},
            headers=API_HEADERS,
        )
        created = client.post(
            "/v3/domains", json={"name": "example.com"}, headers=API_HEADERS
        )
        assert created.status_code == 201
        audit_before = client.get("/v3/audit-events", headers=API_HEADERS).json()
        invalid_patch = client.patch(
            "/v3/domains/example.com",
            json={"http3_enabled": True},
            headers=API_HEADERS,
        )
        assert invalid_create.status_code == invalid_patch.status_code == 422
        assert invalid_patch.json() == invalid_create.json()
        issue = invalid_patch.json()["detail"][0]
        assert set(issue) == {"type", "loc", "msg"}
        assert issue["type"] == "value_error"
        assert issue["loc"][0] == "body"
        assert "http3_enabled" in issue["msg"]
        assert (
            client.get("/v3/domains/example.com", headers=API_HEADERS).json()
            == created.json()
        )
        assert (
            client.get("/v3/audit-events", headers=API_HEADERS).json() == audit_before
        )


def test_every_route_documents_the_runtime_validation_shape(settings):
    schema = control_plane_app(settings).openapi()
    for operations in schema["paths"].values():
        for operation in operations.values():
            assert set(operation["responses"]["422"]["content"]) == {"application/json"}
            assert operation["responses"]["422"]["content"]["application/json"][
                "schema"
            ] == {"$ref": "#/components/schemas/ValidationErrorResponse"}
    components = schema["components"]["schemas"]
    assert components["ValidationErrorResponse"]["properties"]["detail"]["items"] == {
        "$ref": "#/components/schemas/ValidationIssue"
    }
    assert set(components["ValidationIssue"]["required"]) == {"type", "loc", "msg"}
    assert set(components["ValidationIssue"]["properties"]) == {"type", "loc", "msg"}


def test_invalid_response_is_a_server_error(settings):
    app = control_plane_app(settings)

    @app.get("/test-invalid-response", response_model=int, include_in_schema=False)
    def invalid_response():
        return "private-server-value"

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/test-invalid-response")
    assert response.status_code == 500
    assert "private-server-value" not in response.text
