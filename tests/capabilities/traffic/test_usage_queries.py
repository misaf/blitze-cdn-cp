"""Persisted traffic is filtered and paginated through both public interfaces."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import event
from support.cli import runner
from support.control_plane import (
    API_HEADERS,
    cli_control_plane,
    control_plane_app,
    serve,
)

from blitzecdn.capabilities.traffic.domain import (
    CollectedUsage,
    HourlyUsage,
    LogCursor,
    UsageBatch,
    UsageQuery,
)
from blitzecdn.composition import Repository
from blitzecdn.entrypoints.cli import main as cli

START = "2026-10-27T09:00:00Z"
END = "2026-10-27T11:00:00Z"
HOUR = datetime(2026, 10, 27, 9, tzinfo=UTC)


def seed(store):
    # No edge/domain configuration is needed: historical usage stands alone.
    for edge in ("retired-edge-b", "edge-a"):
        store.apply(
            edge,
            CollectedUsage(
                batches=(
                    UsageBatch(
                        end=LogCursor(stream_id="a" * 64, anchor="b" * 64, offset=100),
                        buckets=tuple(
                            HourlyUsage(
                                hostname=hostname,
                                hour=HOUR + timedelta(hours=hour),
                                requests=hour + 1,
                                bytes_sent=2**53 + 1,
                                body_bytes_sent=100,
                            )
                            for hour in range(3)
                            for hostname in ("api.example.com", "example.com")
                        ),
                    ),
                )
            ),
        )


def test_database_filters_boundaries_order_and_sql_pagination(settings):
    repository = Repository(settings.database_path)
    seed(repository.traffic)
    statements = []
    event.listen(
        repository.database.engine,
        "before_cursor_execute",
        lambda _c, _cur, statement, _p, _ctx, _many: statements.append(statement),
    )
    query = UsageQuery.model_validate({"start": START, "end": END})
    full = repository.traffic.usage(query)
    assert full.total == 8
    assert [(row.hour, row.hostname, row.edge) for row in full.items] == sorted(
        (row.hour, row.hostname, row.edge) for row in full.items
    )
    seen = []
    for offset in range(0, 8, 3):
        page = repository.traffic.usage(
            query.model_copy(update={"limit": 3, "offset": offset})
        )
        assert page.total == 8
        seen.extend(page.items)
    assert seen == full.items
    assert any("LIMIT" in statement for statement in statements)
    assert any("count(" in statement.lower() for statement in statements)
    filtered = repository.traffic.usage(
        query.model_copy(
            update={"hostname": "api.example.com", "edge": "retired-edge-b"}
        )
    )
    assert filtered.total == 2
    assert filtered.items[0].bytes_sent == 2**53 + 1
    assert repository.traffic.usage(query.model_copy(update={"offset": 99})).items == []
    assert (
        repository.traffic.usage(
            query.model_copy(update={"hostname": "missing.example"})
        ).total
        == 0
    )
    repository.close()


@pytest.fixture
def usage_client(settings, monkeypatch):
    control = cli_control_plane(settings, monkeypatch)
    repository = control.container.get(Repository)
    seed(repository.traffic)
    serve(monkeypatch, control)
    with TestClient(control_plane_app(settings)) as client:
        yield client
    control.container.close()


def test_api_authorization_pagination_and_cli_match(usage_client):
    client = usage_client
    params = {
        "start": START,
        "end": END,
        "hostname": "api.example.com",
        "limit": 1,
        "offset": 1,
    }
    assert client.get("/v3/traffic/usage", params=params).status_code == 401
    response = client.get("/v3/traffic/usage", params=params, headers=API_HEADERS)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["total"] == 4
    assert result["limit"] == result["offset"] == 1
    assert result["items"][0]["edge"] == "retired-edge-b"
    assert result["items"][0]["bytes_sent"] == 2**53 + 1
    command = runner.invoke(
        cli.app,
        [
            "traffic",
            "usage",
            "--start",
            START,
            "--end",
            END,
            "--hostname",
            "api.example.com",
            "--limit",
            "1",
            "--offset",
            "1",
            "--json",
        ],
    )
    assert command.exit_code == 0, command.output
    assert json.loads(command.output) == result
    assert client.post("/v3/traffic/usage", headers=API_HEADERS).status_code == 405


@pytest.mark.parametrize(
    "changes",
    [
        {"start": "2026-10-27T09:00:00"},
        {"end": "2026-10-27T11:30:00Z"},
        {"end": START},
        {"end": "2026-10-26T00:00:00Z"},
        {"hostname": "*.example.com"},
        {"edge": ""},
        {"limit": 501},
        {"offset": -1},
        {"start": "invalid"},
    ],
)
def test_invalid_api_queries_are_422(usage_client, changes):
    response = usage_client.get(
        "/v3/traffic/usage",
        params={"start": START, "end": END, **changes},
        headers=API_HEADERS,
    )
    assert response.status_code == 422, response.text
    assert all(error["loc"][0] == "query" for error in response.json()["detail"])


def test_required_range_timezone_normalization_and_empty_result(usage_client):
    assert usage_client.get("/v3/traffic/usage", headers=API_HEADERS).status_code == 422
    result = usage_client.get(
        "/v3/traffic/usage",
        params={
            "start": "2026-10-27T12:30:00+03:30",
            "end": END,
            "edge": "edge-a",
        },
        headers=API_HEADERS,
    )
    assert result.status_code == 200, result.text
    assert result.json()["total"] == 4
    empty = usage_client.get(
        "/v3/traffic/usage",
        params={
            "start": END,
            "end": "2026-10-28T00:00:00Z",
            "edge": "missing",
        },
        headers=API_HEADERS,
    )
    assert empty.json() == {"items": [], "total": 0, "limit": 100, "offset": 0}
    invalid = runner.invoke(
        cli.app, ["traffic", "usage", "--start", "invalid", "--end", END]
    )
    # CliRunner invokes app directly; the executable maps this exception to 2.
    assert isinstance(invalid.exception, ValidationError)
    assert cli._exit_code(invalid.exception) == 2
