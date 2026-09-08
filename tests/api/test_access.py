"""Public access is an IP gate in addition to existing API authentication."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from control_plane_fixtures import control_plane_app
from fastapi.testclient import TestClient

from blitzecdn.api import __main__ as server
from blitzecdn.api.access import AllowedIPsMiddleware


@pytest.mark.parametrize(
    "path", ["/docs", "/redoc", "/openapi.json", "/health", "/v1/sites"]
)
@pytest.mark.parametrize("peer", ["198.51.100.8", "2001:db8:2::8", "not-an-ip"])
def test_unlisted_clients_cannot_reach_any_route(settings, path, peer):
    restricted = settings.model_copy(update={"allowed_ips": ("203.0.113.0/24",)})
    with TestClient(control_plane_app(restricted), client=(peer, 1234)) as client:
        response = client.get(
            path,
            headers={
                "X-API-Key": "x" * 32,
                "X-Forwarded-For": "203.0.113.1",
                "Forwarded": "for=127.0.0.1",
                "X-Real-IP": "127.0.0.1",
            },
        )
        assert response.status_code == 403
        assert response.json() == {"detail": "Client IP is not allowed"}


@pytest.mark.parametrize(
    "peer",
    [
        "203.0.113.8",
        # A dual-stack socket reports an allowed IPv4 client in this form.
        "::ffff:203.0.113.8",
        "127.0.0.1",
        "::1",
    ],
)
def test_allowed_clients_can_read_schema_but_still_need_auth(settings, peer):
    restricted = settings.model_copy(update={"allowed_ips": ("203.0.113.0/24",)})
    with TestClient(control_plane_app(restricted), client=(peer, 1234)) as client:
        assert client.get("/openapi.json").status_code == 200
        assert client.get("/v1/sites").status_code == 401
        assert (
            client.get("/v1/sites", headers={"X-API-Key": "x" * 32}).status_code == 200
        )


@pytest.mark.parametrize(
    ("allowed_ips", "host"),
    [((), "127.0.0.1"), (("203.0.113.8/32",), "0.0.0.0")],  # noqa: S104 - verifies opt-in public binding
)
def test_installed_server_derives_binding_and_disables_proxy_headers(
    settings, monkeypatch, allowed_ips, host
):
    configured = settings.model_copy(update={"allowed_ips": allowed_ips})
    monkeypatch.setattr(server.Settings, "from_environment", lambda: configured)
    run = MagicMock()
    monkeypatch.setattr(server.uvicorn, "run", run)
    server.main()
    assert run.call_args.kwargs == {"host": host, "port": 8000, "proxy_headers": False}
    assert run.call_args.args[0].state.settings is configured


def test_missing_transport_peer_is_denied_and_lifespan_is_forwarded():
    app = AsyncMock()
    middleware = AllowedIPsMiddleware(app, ("203.0.113.8/32",))
    receive, send = AsyncMock(), AsyncMock()
    asyncio.run(middleware({"type": "http", "client": None}, receive, send))
    assert send.call_args_list[0].args[0]["status"] == 403
    app.assert_not_called()
    scope = {"type": "lifespan"}
    asyncio.run(middleware(scope, receive, send))
    app.assert_awaited_once_with(scope, receive, send)


def test_unlisted_websocket_is_closed_before_application_runs():
    app = AsyncMock()
    middleware = AllowedIPsMiddleware(app, ("203.0.113.8/32",))
    receive, send = AsyncMock(), AsyncMock()
    asyncio.run(
        middleware(
            {"type": "websocket", "client": ("198.51.100.8", 1234)}, receive, send
        )
    )
    send.assert_awaited_once_with({"type": "websocket.close", "code": 1008})
    app.assert_not_called()
