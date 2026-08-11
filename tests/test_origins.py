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

from blitzecdn.domain.sites import CdnSite, HttpScheme
from blitzecdn.infrastructure.origins import OriginProbe


def _site(**overrides) -> CdnSite:
    return CdnSite.model_validate(
        {
            "name": "cdn-example-com",
            "server_names": ["cdn.example.com"],
            "origin_host": "127.0.0.1",
            "origin_scheme": HttpScheme.HTTP,
            **overrides,
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


def test_a_listening_http_origin_is_reachable(settings):
    with _listener() as port:
        result = OriginProbe(settings).check(_site(origin_port=port))

    assert result.resolved is True
    assert result.reachable is True
    assert result.tls_verified is None, "TLS is not a question for an http origin"
    assert result.ok is True


def test_a_closed_port_is_resolved_but_not_reachable(settings):
    # Bind and close so the port is almost certainly free and refuses.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    result = OriginProbe(settings).check(_site(origin_port=port))

    assert result.resolved is True
    assert result.reachable is False
    assert result.ok is False
    assert result.detail and "cannot connect" in result.detail


def test_an_origin_that_does_not_resolve_says_so(settings):
    result = OriginProbe(settings).check(
        _site(origin_host="origin.invalid", origin_port=443)
    )

    assert result.resolved is False
    assert result.reachable is False
    assert result.detail and "does not resolve" in result.detail


def test_an_https_origin_with_an_unverifiable_certificate_fails_tls(
    settings, tmp_path, certificate_pair
):
    """A self-signed origin certificate is exactly the case worth catching."""
    certificate, key = certificate_pair(("origin.example.com",))
    certificate_file = tmp_path / "origin.pem"
    certificate_file.write_bytes(certificate + key)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate_file)

    with _listener(tls_context=context) as port:
        result = OriginProbe(settings).check(
            _site(
                origin_scheme=HttpScheme.HTTPS,
                origin_port=port,
                origin_sni="origin.example.com",
            )
        )

    assert result.reachable is True, "the TCP connection itself succeeded"
    assert result.tls_verified is False
    assert result.ok is False
    assert result.sni == "origin.example.com"


def test_the_probe_uses_the_same_sni_the_edge_template_would(settings):
    """site.conf.j2 falls back origin_sni -> origin_request_host -> origin_host."""
    probe = OriginProbe(settings)
    with _listener() as port:
        base = {"origin_scheme": HttpScheme.HTTPS, "origin_port": port}
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
        origin_scheme=HttpScheme.HTTPS,
    )
    assert site.effective_origin_sni == "origin.example.com"
    assert OriginProbe(settings).check(site).sni == "origin.example.com"


def test_checking_no_sites_does_no_work(settings):
    assert OriginProbe(settings).check_all([]) == []


@pytest.mark.parametrize(
    ("scheme", "expected_port"),
    [(HttpScheme.HTTP, "80"), (HttpScheme.HTTPS, "443")],
)
def test_an_origin_without_a_port_uses_the_scheme_default(
    settings, scheme, expected_port
):
    result = OriginProbe(settings).check(
        _site(origin_host="origin.invalid", origin_scheme=scheme)
    )
    assert result.origin.endswith(f":{expected_port}")
