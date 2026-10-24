"""The verification phase against a real socket, not a double.

Every other test of a deployment replaces this probe, because a unit test must
not open a connection. This one is the exception and has to be: what the phase
claims is that an edge *answered over the network*, and a fake that returned
`served=True` would prove that claim about itself rather than about any HTTP.

The server is a disposable one this module starts on loopback and stops again.
"""

from __future__ import annotations

import http.server
import ipaddress
import ssl
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from policy_support import site_from_settings

from blitzecdn.capabilities.deployments.adapters.serving import (
    ServingProbe,
    _certificate_covers,
)
from blitzecdn.capabilities.serving.domain import CdnSite


class _Recorder(http.server.BaseHTTPRequestHandler):
    """Answers 200 and remembers the Host header it was asked for."""

    seen: ClassVar[list[str]] = []
    release: ClassVar[str] = "a" * 64

    def do_GET(self) -> None:
        type(self).seen.append(self.headers.get("Host", ""))
        self.send_response(200)
        payload = (type(self).release + "\n").encode("ascii")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:
        """Silence the default stderr access log."""


@pytest.fixture
def edge_server() -> Iterator[int]:
    _Recorder.seen = []
    _Recorder.release = "a" * 64
    server = http.server.HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def site(**policy: object) -> CdnSite:
    return site_from_settings(
        {
            "name": "example-com",
            "server_names": ["cdn.example.com"],
            "origin_host": "198.51.100.10",
            **policy,
        }
    )


def probe_on(port: int) -> ServingProbe:
    """A probe pointed at the disposable server rather than at port 80.

    The port comes from the scheme in production — that is the contract, and
    nothing here is trying to change it — so this overrides the one thing a
    test cannot control about a loopback server.
    """

    class LoopbackProbe(ServingProbe):
        def _connection(self, scheme, address, _port, hostname):
            return super()._connection(scheme, address, port, hostname)

    return LoopbackProbe(timeout=5.0)


def test_an_edge_that_answers_is_reported_as_serving(edge_server):
    (result,) = probe_on(edge_server).verify(
        edge="edge-a", address="127.0.0.1", release_digest="a" * 64, sites=[site()]
    )

    assert result.served is True
    assert result.detail == "HTTP 200"
    assert result.hostname == "cdn.example.com"


def test_the_request_is_addressed_to_the_hostname_not_to_the_edge(edge_server):
    """DNS usually does not answer with this edge yet when a deploy finishes.

    Resolving the hostname would test the DNS transition rather than the edge,
    so the connection goes to the address and the name travels in the header.
    """
    probe_on(edge_server).verify(
        edge="edge-a", address="127.0.0.1", release_digest="a" * 64, sites=[site()]
    )

    assert _Recorder.seen == ["cdn.example.com"]


def test_an_edge_that_is_not_listening_is_reported_as_not_serving():
    """The failure the phase exists to catch, and it is not an exception.

    A configuration that `nginx -t` accepted and a reload that returned zero
    can still leave nothing answering; the probe says so rather than raising,
    so the rollout can attribute it to the edge and the phase.
    """

    # Port 9 is discard, which nothing listens on; a connection is refused
    # immediately rather than hanging.
    class ClosedPortProbe(ServingProbe):
        def _connection(self, scheme, address, _port, hostname):
            return super()._connection(scheme, address, 9, hostname)

    (result,) = ClosedPortProbe(timeout=2.0).verify(
        edge="edge-a", address="127.0.0.1", release_digest="a" * 64, sites=[site()]
    )

    assert result.served is False
    assert result.detail


def test_a_site_with_no_hostname_is_not_probed(edge_server):
    """It contributes no server block, so there is no name to ask for."""
    assert (
        probe_on(edge_server).verify(
            edge="edge-a",
            address="127.0.0.1",
            release_digest="a" * 64,
            sites=[site(server_names=[])],
        )
        == ()
    )


def test_one_request_per_site_rather_than_one_per_hostname(edge_server):
    """A site's hostnames share one server block, so the rest prove nothing.

    A fleet with a thousand hostnames would otherwise turn every deployment
    into a thousand requests.
    """
    probe_on(edge_server).verify(
        edge="edge-a",
        address="127.0.0.1",
        release_digest="a" * 64,
        sites=[site(server_names=["cdn.example.com", "www.example.com"])],
    )

    assert _Recorder.seen == ["cdn.example.com"]


# -- The certificate a TLS site presents -----------------------------------


def _certificate(*names: str, san: bool = True) -> tuple[bytes, bytes]:
    """A self-signed certificate and its key, PEM, naming ``names``.

    Self-signed on purpose: the probe does not verify the chain, and a test
    that needed a CA would be testing something the probe does not claim.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])])
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
    )
    if san:
        entries: list[x509.GeneralName] = []
        for name in names:
            try:
                entries.append(x509.IPAddress(ipaddress.ip_address(name)))
            except ValueError:
                entries.append(x509.DNSName(name))
        builder = builder.add_extension(
            x509.SubjectAlternativeName(entries), critical=False
        )
    certificate = builder.sign(key, hashes.SHA256())
    return (
        certificate.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


def _der(*names: str, san: bool = True) -> bytes:
    pem, _key = _certificate(*names, san=san)
    return x509.load_pem_x509_certificate(pem).public_bytes(serialization.Encoding.DER)


class _QuietServer(http.server.HTTPServer):
    """Drops the traceback a client that hangs up mid-request leaves behind.

    Which is what the probe does, correctly, to an edge whose certificate does
    not name the site: it closes after the handshake and sends no request.
    """

    def handle_error(self, request: object, client_address: object) -> None:
        """Silence the default stderr report."""


@pytest.fixture
def tls_edge(tmp_path) -> Iterator[Callable[..., int]]:
    """Start a loopback HTTPS edge presenting a certificate for given names."""
    servers: list[tuple[http.server.HTTPServer, threading.Thread]] = []

    def start(*names: str) -> int:
        pem, key = _certificate(*names)
        (tmp_path / "edge.pem").write_bytes(pem)
        (tmp_path / "edge.key").write_bytes(key)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(tmp_path / "edge.pem", tmp_path / "edge.key")
        server = _QuietServer(("127.0.0.1", 0), _Recorder)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return server.server_address[1]

    _Recorder.seen = []
    _Recorder.release = "a" * 64
    try:
        yield start
    finally:
        for server, thread in servers:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def tls_site() -> CdnSite:
    return site(
        ssl_mode="full",
        certificate_mode="existing",
        certificate_path="/etc/ssl/edge.pem",
        certificate_key_path="/etc/ssl/edge.key",
    )


def test_an_edge_presenting_the_sites_certificate_is_serving(tls_edge):
    port = tls_edge("cdn.example.com")

    (result,) = probe_on(port).verify(
        edge="edge-a", address="127.0.0.1", release_digest="a" * 64, sites=[tls_site()]
    )

    assert result.served is True
    assert result.detail == "HTTP 200"


def test_an_edge_presenting_another_sites_certificate_is_not_serving(tls_edge):
    """The case the probe's docstring used to claim the handshake caught.

    With the chain unverified nothing in the handshake looks at the
    certificate, so an edge presenting the wrong one — a swapped path, a stale
    file — answered 200 and passed. It is now refused before the request.
    """
    port = tls_edge("other.example.net")

    (result,) = probe_on(port).verify(
        edge="edge-a", address="127.0.0.1", release_digest="a" * 64, sites=[tls_site()]
    )

    assert result.served is False
    assert "does not name cdn.example.com" in result.detail
    assert _Recorder.seen == []


@pytest.mark.parametrize(
    ("names", "hostname", "covered"),
    [
        (("cdn.example.com",), "CDN.example.com.", True),
        (("*.example.com",), "cdn.example.com", True),
        (("*.example.com",), "example.com", False),
        (("*.example.com",), "a.cdn.example.com", False),
        (("*.example.com",), "*.example.com", True),
        (("cdn.example.com",), "*.example.com", False),
        (("192.0.2.10",), "192.0.2.10", True),
        (("192.0.2.10",), "192.0.2.11", False),
    ],
)
def test_which_names_a_certificate_covers(names, hostname, covered):
    assert _certificate_covers(_der(*names), hostname) is covered


def test_a_name_only_in_the_subject_is_not_one_the_certificate_covers():
    """No current client reads the CN, so a probe that did would pass an edge
    every visitor's browser refuses."""
    der = _der("cdn.example.com", san=False)
    assert _certificate_covers(der, "cdn.example.com") is False


def test_a_reachable_edge_serving_an_older_release_is_refused(edge_server):
    _Recorder.release = "b" * 64
    (result,) = probe_on(edge_server).verify(
        edge="edge-a", address="127.0.0.1", sites=[site()], release_digest="a" * 64
    )
    assert result.served is False
    assert "expected release" in result.detail
