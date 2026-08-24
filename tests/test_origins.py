"""Exercise the origin probe against real local sockets.

Real sockets rather than mocks: the whole value of this check is that it makes
the same connection the edge will, and a mocked `socket` would only prove the
probe calls the functions the test expects it to call.
"""

from __future__ import annotations

import socket
import ssl
import threading
from contextlib import contextmanager

import pytest
from pydantic import ValidationError

import blitzecdn.infrastructure.origins as origins_module
from blitzecdn.domain.origins import OriginCheck
from blitzecdn.domain.sites import CdnSite, HttpScheme, SslMode
from blitzecdn.infrastructure.origins import OriginProbe


def _site(**overrides) -> CdnSite:
    payload = {
        "name": "cdn-example-com",
        "server_names": ["cdn.example.com"],
        "origin_host": "127.0.0.1",
        **overrides,
    }
    if payload.get("ssl_mode", SslMode.OFF) != SslMode.OFF:
        payload |= {
            "certificate_mode": "existing",
            "certificate_path": "/etc/ssl/certs/edge.pem",
            "certificate_key_path": "/etc/ssl/private/edge.key",
        }
    return CdnSite.model_validate(payload)


def test_origin_reports_require_the_current_ssl_mode():
    with pytest.raises(ValidationError, match="ssl_mode"):
        OriginCheck.model_validate(
            {
                "site": "cdn-example-com",
                "origin": "origin.example.com:443",
                "scheme": "https",
            }
        )


@contextmanager
def _listener(*, tls_context: ssl.SSLContext | None = None):
    """A socket that accepts one connection and immediately drops it."""
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def serve() -> None:
        try:
            connection, _ = server.accept()
        except OSError:
            return
        try:
            if tls_context is not None:
                with tls_context.wrap_socket(connection, server_side=True):
                    pass
            else:
                connection.close()
        except (OSError, ssl.SSLError):
            pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield server.getsockname()[1]
    finally:
        server.close()
        thread.join(timeout=2)


def test_a_listening_http_origin_is_reachable(settings, monkeypatch):
    with _listener() as port:
        monkeypatch.setitem(origins_module._DEFAULT_PORTS, HttpScheme.HTTP, port)
        result = OriginProbe(settings).check(_site())

    assert result.reachable is True
    assert result.tls_verified is None, "TLS is not a question for an http origin"
    assert result.ok is True


def test_a_closed_port_is_not_reachable(settings, monkeypatch):
    # Bind and close so the port is almost certainly free and refuses.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    monkeypatch.setitem(origins_module._DEFAULT_PORTS, HttpScheme.HTTP, port)
    result = OriginProbe(settings).check(_site())

    assert result.reachable is False
    assert result.ok is False
    assert result.detail and "cannot connect" in result.detail


def test_an_origin_that_does_not_resolve_says_so(settings):
    result = OriginProbe(settings).check(_site(origin_host="origin.invalid"))

    assert result.reachable is False
    assert result.detail and "does not resolve" in result.detail


def test_an_https_origin_with_an_unverifiable_certificate_fails_tls(
    settings, tmp_path, certificate_pair, monkeypatch
):
    """A self-signed origin certificate is exactly the case worth catching."""
    certificate, key = certificate_pair(("origin.example.com",))
    certificate_file = tmp_path / "origin.pem"
    certificate_file.write_bytes(certificate + key)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate_file)

    with _listener(tls_context=context) as port:
        monkeypatch.setitem(origins_module._DEFAULT_PORTS, HttpScheme.HTTPS, port)
        result = OriginProbe(settings).check(
            _site(
                ssl_mode=SslMode.FULL_STRICT,
                origin_sni="origin.example.com",
            )
        )

    assert result.reachable is True, "the TCP connection itself succeeded"
    assert result.tls_verified is False
    assert result.ok is False
    assert result.sni == "origin.example.com"


def test_full_accepts_an_unverifiable_origin_certificate(
    settings, tmp_path, certificate_pair, monkeypatch
):
    certificate, key = certificate_pair(("origin.example.com",))
    certificate_file = tmp_path / "origin-full.pem"
    certificate_file.write_bytes(certificate + key)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate_file)

    with _listener(tls_context=context) as port:
        monkeypatch.setitem(origins_module._DEFAULT_PORTS, HttpScheme.HTTPS, port)
        result = OriginProbe(settings).check(
            _site(
                ssl_mode=SslMode.FULL,
                origin_sni="origin.example.com",
            )
        )

    assert result.reachable is True
    assert result.tls_verified is None
    assert result.ok is True


def test_the_probe_uses_the_same_sni_the_edge_template_would(settings, monkeypatch):
    """site.conf.j2 falls back origin_sni -> origin_request_host -> origin_host."""
    probe = OriginProbe(settings)
    with _listener() as port:
        monkeypatch.setitem(origins_module._DEFAULT_PORTS, HttpScheme.HTTPS, port)
        base = {"ssl_mode": SslMode.FULL}
        assert probe.check(_site(**base, origin_sni="a.example.com")).sni == (
            "a.example.com"
        )
        assert (
            probe.check(_site(**base, origin_request_host="b.example.com")).sni
            == "b.example.com"
        )
        assert probe.check(_site(**base)).sni == "127.0.0.1"


def test_a_wildcard_server_name_is_never_sent_as_sni(settings):
    """A wildcard is legal in server_name and meaningless in a handshake.

    `*.example.com` matches no certificate, so falling back to it would make
    every wildcard site fail verification against an origin that is otherwise
    correctly configured.
    """
    site = _site(
        server_names=["*.example.com", "example.com"],
        origin_host="origin.example.com",
        ssl_mode=SslMode.FULL_STRICT,
    )
    assert site.effective_origin_sni == "origin.example.com"
    assert OriginProbe(settings).check(site).sni == "origin.example.com"


def test_an_origin_is_rendered_once_for_whoever_connects_to_it(settings):
    """The edges do the connecting now, and are told what to connect to here.

    The port and the SNI are decided in one place so the fleet's probe and the
    controller's advisory one cannot disagree about what a site's origin is.
    """
    site = _site(origin_host="origin.example.com", ssl_mode=SslMode.FULL_STRICT)
    rendered = OriginProbe(settings).to_probe(site)

    assert rendered == {
        "name": site.name,
        "origin_host": "origin.example.com",
        "origin_port": 443,
        "ssl_mode": "full_strict",
        "origin_tls_verify": True,
        "origin_sni": site.effective_origin_sni,
    }


@pytest.mark.parametrize(
    ("mode", "expected_port"),
    [(SslMode.OFF, "80"), (SslMode.FULL_STRICT, "443")],
)
def test_an_origin_without_a_port_uses_the_scheme_default(
    settings, mode, expected_port
):
    result = OriginProbe(settings).check(
        _site(origin_host="origin.invalid", ssl_mode=mode)
    )
    assert result.origin.endswith(f":{expected_port}")
