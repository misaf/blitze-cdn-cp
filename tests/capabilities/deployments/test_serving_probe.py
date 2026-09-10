"""The verification phase against a real socket, not a double.

Every other test of a deployment replaces this probe, because a unit test must
not open a connection. This one is the exception and has to be: what the phase
claims is that an edge *answered over the network*, and a fake that returned
`served=True` would prove that claim about itself rather than about any HTTP.

The server is a disposable one this module starts on loopback and stops again.
"""

from __future__ import annotations

import http.server
import threading
from collections.abc import Iterator
from typing import ClassVar

import pytest

from blitzecdn.capabilities.deployments.adapters.serving import ServingProbe
from blitzecdn.capabilities.dns.domain import CdnSite


class _Recorder(http.server.BaseHTTPRequestHandler):
    """Answers 200 and remembers the Host header it was asked for."""

    seen: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        type(self).seen.append(self.headers.get("Host", ""))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: object) -> None:
        """Silence the default stderr access log."""


@pytest.fixture
def edge_server() -> Iterator[int]:
    _Recorder.seen = []
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
    return CdnSite.model_validate(
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
        edge="edge-a", address="127.0.0.1", sites=[site()]
    )

    assert result.served is True
    assert result.detail == "HTTP 200"
    assert result.hostname == "cdn.example.com"


def test_the_request_is_addressed_to_the_hostname_not_to_the_edge(edge_server):
    """DNS usually does not answer with this edge yet when a deploy finishes.

    Resolving the hostname would test the DNS transition rather than the edge,
    so the connection goes to the address and the name travels in the header.
    """
    probe_on(edge_server).verify(edge="edge-a", address="127.0.0.1", sites=[site()])

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
        edge="edge-a", address="127.0.0.1", sites=[site()]
    )

    assert result.served is False
    assert result.detail


def test_a_site_with_no_hostname_is_not_probed(edge_server):
    """It contributes no server block, so there is no name to ask for."""
    assert (
        probe_on(edge_server).verify(
            edge="edge-a", address="127.0.0.1", sites=[site(server_names=[])]
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
        sites=[site(server_names=["cdn.example.com", "www.example.com"])],
    )

    assert _Recorder.seen == ["cdn.example.com"]
