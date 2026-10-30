"""Reject excessive bodies before authentication and clean up partial uploads."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from starlette import formparsers
from support.control_plane import control_plane_app

from blitzecdn.entrypoints.api.body_limit import RequestBodyLimitMiddleware


def _request(
    settings,
    chunks,
    *,
    content_type="application/json",
    length=None,
    peer="203.0.113.8",
    path="/v3/domains",
):
    app = control_plane_app(settings)
    messages = []
    consumed = []
    pending = iter(chunks)

    async def receive():
        chunk = next(pending)
        consumed.append(len(chunk))
        return {
            "type": "http.request",
            "body": chunk,
            "more_body": len(consumed) < len(chunks),
        }

    async def send(message):
        messages.append(message)

    headers = [(b"content-type", content_type.encode())]
    if length is not None:
        headers.append((b"content-length", str(length).encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": (peer, 1234),
        "server": ("audit", 80),
    }
    with TestClient(app):
        asyncio.run(app(scope, receive, send))
    return messages[0]["status"], consumed


@pytest.mark.parametrize("length", [None, 1, 16])
def test_stream_overflow_stops_at_first_excess_chunk(settings, length):
    configured = settings.model_copy(update={"api_max_body_bytes": 16})
    status, consumed = _request(configured, [b" " * 16, b"x", b"unread"], length=length)
    assert status == 413
    assert consumed == [16, 1]


@pytest.mark.parametrize("length", [None, 16])
def test_exact_limit_reaches_authentication(settings, length):
    configured = settings.model_copy(update={"api_max_body_bytes": 16})
    status, consumed = _request(configured, [b"{}", b" " * 14], length=length)
    assert status == 401
    assert consumed == [2, 14]


@pytest.mark.parametrize("peer,status", [("203.0.113.8", 413), ("198.51.100.8", 403)])
def test_declared_overflow_reads_nothing_and_preserves_ip_gate(settings, peer, status):
    configured = settings.model_copy(
        update={
            "api_max_body_bytes": 16,
            "allowed_ips": ("203.0.113.8/32",),
        }
    )
    actual, consumed = _request(configured, [b"unread"], length=17, peer=peer)
    assert actual == status
    assert consumed == []


@pytest.mark.parametrize("length", ["invalid", -1])
def test_invalid_content_length_is_rejected(settings, length):
    status, consumed = _request(settings, [b"unread"], length=length)
    assert status == 400
    assert consumed == []


def _file_header(name):
    return (
        f'--audit\r\nContent-Disposition: form-data; name="{name}"; '
        f'filename="{name}.pem"\r\n\r\n'
    ).encode()


def test_default_budget_fits_certificate_and_key_limits(settings):
    chunks = [
        _file_header("certificate"),
        b"x" * 1_048_576,
        b"\r\n",
        _file_header("private_key"),
        b"x" * 262_144,
        b"\r\n--audit--\r\n",
    ]
    status, consumed = _request(
        settings,
        chunks,
        content_type="multipart/form-data; boundary=audit",
        path="/v3/hosts/example-com/certificate/upload",
        length=sum(map(len, chunks)),
    )
    assert status == 401
    assert sum(consumed) == sum(map(len, chunks))


def test_multipart_overflow_closes_spooled_files_and_stops_reading(
    settings, monkeypatch
):
    files = []
    original = formparsers.SpooledTemporaryFile

    def tracked_file(*args, **kwargs):
        file = original(*args, **kwargs)
        files.append(file)
        return file

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", tracked_file)
    chunks = [
        _file_header("certificate"),
        b"x" * 1_048_577,
        b"x" * 1_048_576,
        b"unread",
    ]
    status, consumed = _request(
        settings,
        chunks,
        content_type="multipart/form-data; boundary=audit",
        path="/v3/hosts/example-com/certificate/upload",
    )
    assert status == 413
    assert len(consumed) == 3
    assert files and all(file.closed for file in files)
    assert files[0]._rolled


@pytest.mark.parametrize("scope_type", ["lifespan", "websocket"])
def test_non_http_scopes_are_forwarded(scope_type):
    app, receive, send = AsyncMock(), AsyncMock(), AsyncMock()
    scope = {"type": scope_type}
    asyncio.run(RequestBodyLimitMiddleware(app, 16)(scope, receive, send))
    app.assert_awaited_once_with(scope, receive, send)
