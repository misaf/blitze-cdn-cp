"""OpenAPI and request validation agree about omission, null, and bounds."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from support.control_plane import API_HEADERS, control_plane_app

from blitzecdn.capabilities.dns.api.models import DomainPatch, RecordPatch, RulePatch
from blitzecdn.capabilities.edges.api.models import EdgePatch

PATCHES = [
    (
        DomainPatch,
        {
            "origin_request_host",
            "origin_sni",
            "certificate_path",
            "certificate_key_path",
        },
    ),
    (RecordPatch, set()),
    (RulePatch, set()),
    (EdgePatch, {"private_key_file", "capabilities"}),
]


def _allows_null(schema, schemas):
    if "$ref" in schema:
        return _allows_null(schemas[schema["$ref"].rsplit("/", 1)[-1]], schemas)
    return schema.get("type") == "null" or any(
        _allows_null(part, schemas) for part in schema.get("anyOf", [])
    )


@pytest.mark.parametrize(("model", "nullable"), PATCHES)
def test_patch_fields_are_omittable_and_only_clearable_values_accept_null(
    settings, model, nullable
):
    app = control_plane_app(settings)
    schemas = app.openapi()["components"]["schemas"]
    schema = schemas[model.__name__]
    assert not schema.get("required")
    assert model().to_domain().model_dump(exclude_unset=True) == {}

    for name, field in schema["properties"].items():
        assert _allows_null(field, schemas) == (name in nullable), name
        if name in nullable:
            patch = model.model_validate({name: None})
            assert patch.to_domain().model_dump(exclude_unset=True) == {name: None}
        else:
            assert "default" not in field, name
            with pytest.raises(ValidationError) as rejected:
                model.model_validate({name: None})
            assert rejected.value.errors()[0]["loc"] == (name,)


@pytest.mark.parametrize(
    ("model", "field", "minimum", "maximum"),
    [
        (RecordPatch, "ttl", 1, 604800),
        (EdgePatch, "port", 1, 65535),
        (RulePatch, "priority", 1, 1000),
    ],
)
def test_patch_numeric_bounds_are_published_and_enforced(
    settings, model, field, minimum, maximum
):
    schema = control_plane_app(settings).openapi()["components"]["schemas"]
    published = schema[model.__name__]["properties"][field]
    assert published["minimum"] == minimum
    assert published["maximum"] == maximum
    for value in (minimum, maximum):
        assert getattr(model.model_validate({field: value}), field) == value
    for value in (minimum - 1, maximum + 1):
        with pytest.raises(ValidationError):
            model.model_validate({field: value})


def test_invalid_patch_nulls_return_field_errors_without_mutating_resources(settings):
    with TestClient(control_plane_app(settings)) as client:
        assert (
            client.post(
                "/v3/domains", json={"name": "example.com"}, headers=API_HEADERS
            ).status_code
            == 201
        )
        assert (
            client.post(
                "/v3/domains/example.com/records",
                json={"domain": "example.com", "name": "www", "value": "192.0.2.10"},
                headers=API_HEADERS,
            ).status_code
            == 201
        )
        assert (
            client.post(
                "/v3/domains/example.com/rules",
                json={"name": "no-cache", "overrides": {"cache_enabled": False}},
                headers=API_HEADERS,
            ).status_code
            == 201
        )
        assert (
            client.post(
                "/v3/edges",
                json={"name": "patch-edge", "host": "192.0.2.20"},
                headers=API_HEADERS,
            ).status_code
            == 201
        )

        resources = [
            ("/v3/domains/example.com", DomainPatch, PATCHES[0][1]),
            ("/v3/domains/example.com/records/www", RecordPatch, set()),
            ("/v3/domains/example.com/rules/no-cache", RulePatch, set()),
            ("/v3/edges/patch-edge", EdgePatch, PATCHES[3][1]),
        ]
        for path, model, nullable in resources:
            read_path = path if model is not RecordPatch else path.rsplit("/", 1)[0]
            before = client.get(read_path, headers=API_HEADERS).json()
            for field in set(model.model_fields) - nullable:
                response = client.patch(path, json={field: None}, headers=API_HEADERS)
                assert response.status_code == 422, (path, field, response.text)
                assert response.json()["detail"][0]["loc"] == ["body", field]
            assert client.patch(path, json={}, headers=API_HEADERS).status_code == 200
            assert client.get(read_path, headers=API_HEADERS).json() == before
