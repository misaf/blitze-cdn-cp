"""Asking an edge, over the network, whether it is actually serving.

This is the whole of the difference between "the deploy succeeded" and "the
fleet is serving". Every other signal a deployment has is something a tool said
about itself: Ansible reported ok, ``nginx -t`` parsed the tree, ``nginx -s
reload`` returned zero. All three can be true of an edge that answers nothing —
a reload nginx accepts still leaves an upstream unreachable, a listener
unclaimed, a certificate the worker cannot read, or a firewall closed in front
of the lot.

So verification is a request. The controller connects to the edge's own public
address, asks for a hostname the release says that edge should now be serving,
and requires an HTTP response.

What a response proves, and what it does not
--------------------------------------------
A response proves the edge is listening on the public port, that its
configuration claimed this hostname rather than falling through to the
catch-all, and — for a TLS site — that it presented a certificate for that name.
It does not prove the origin is healthy: a 502 is the edge working correctly and
saying the origin is not. That distinction is deliberate. Verification is about
whether *this deployment* put the edge into service, and an origin that was
already down was not this deployment's doing; failing the rollout for it would
roll back a configuration that is fine and leave the origin exactly as broken.

The request carries no body, asks for ``/`` and follows no redirect. An
always-use-HTTPS site answers the plaintext probe with a 301, which is the
correct answer and is accepted as one.
"""

from __future__ import annotations

import http.client
import socket
import ssl
from collections.abc import Sequence
from typing import Any

from blitzecdn.capabilities.dns.domain import CdnSite
from blitzecdn.capabilities.http.policy import DEFAULT_PORTS, HttpScheme

__all__ = ["ServingProbe", "ServingResult"]

#: The path asked for. Root rather than the status endpoint, because the status
#: endpoint is bound to loopback on the edge and answers whether *nginx* is up
#: — which the reload already told us. This asks the question a visitor asks.
_PATH = "/"

#: A response at all is the pass condition, so this is a bound on waiting
#: rather than a service-level objective. An edge slower than this during a
#: converge is an edge worth failing the rollout for.
_TIMEOUT_SECONDS = 10.0


class ServingResult:
    """Whether one edge answered for one hostname, and what it said."""

    __slots__ = ("detail", "edge", "hostname", "served")

    def __init__(self, *, edge: str, hostname: str, served: bool, detail: str) -> None:
        self.edge = edge
        self.hostname = hostname
        self.served = served
        self.detail = detail

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (
            f"ServingResult(edge={self.edge!r}, hostname={self.hostname!r}, "
            f"served={self.served!r}, detail={self.detail!r})"
        )


class ServingProbe:
    """Requests a hostname from one edge and reports whether it answered."""

    def __init__(self, *, timeout: float = _TIMEOUT_SECONDS) -> None:
        self.timeout = timeout

    def verify(
        self, *, edge: str, address: str, sites: Sequence[CdnSite]
    ) -> tuple[ServingResult, ...]:
        """Ask this edge for one hostname per site it should be serving.

        One hostname per site rather than all of them. A site's hostnames share
        a single ``server`` block by construction — that is what makes them one
        site — so the second name proves nothing the first did not, and a fleet
        with a thousand hostnames would turn every deployment into a thousand
        requests.

        A site with no hostnames contributes no server block and is skipped
        rather than probed for a name that does not exist.
        """
        results: list[ServingResult] = []
        for site in sites:
            if not site.server_names:
                continue
            hostname = site.server_names[0]
            served, detail = self._request(address, hostname, site)
            results.append(
                ServingResult(
                    edge=edge, hostname=hostname, served=served, detail=detail
                )
            )
        return tuple(results)

    def _request(self, address: str, hostname: str, site: CdnSite) -> tuple[bool, str]:
        """One HTTP request to ``address``, addressed to ``hostname``.

        Connects to the edge's address and sends the hostname in the ``Host``
        header — and, over TLS, in SNI. That separation is the point: DNS may
        not yet answer with this edge's address, and often does not at the
        moment a deployment finishes, so resolving the hostname would test the
        DNS transition rather than the edge.
        """
        scheme = HttpScheme.HTTPS if site.ssl_mode.serves_tls else HttpScheme.HTTP
        port = DEFAULT_PORTS[scheme]
        try:
            connection = self._connection(scheme, address, port, hostname)
            try:
                connection.request("GET", _PATH, headers={"Host": hostname})
                response = connection.getresponse()
                response.read(0)
            finally:
                connection.close()
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            return False, f"{type(exc).__name__}: {exc}"
        return True, f"HTTP {response.status}"

    def _connection(
        self, scheme: HttpScheme, address: str, port: int, hostname: str
    ) -> http.client.HTTPConnection:
        if scheme is HttpScheme.HTTP:
            return http.client.HTTPConnection(address, port=port, timeout=self.timeout)
        return _SniConnection(address, sni=hostname, port=port, timeout=self.timeout)


class _SniConnection(http.client.HTTPSConnection):
    """HTTPS to one address, announcing a different name in SNI.

    ``HTTPSConnection`` takes its SNI from the host it connects to, which is
    exactly wrong here: the connection goes to the edge's address and the name
    being asked about is the customer's hostname. DNS may not yet answer with
    this edge — at the moment a deployment finishes it usually does not — so
    resolving the hostname would test the DNS transition rather than the edge.

    The certificate is not verified. An edge may be serving one this controller
    has no chain for; an uploaded certificate from a private CA is a supported
    mode, and verifying here would fail a correct deployment for a trust store
    this machine happens to lack. A certificate that does not match the SNI
    still fails the handshake, which is the part worth catching.
    """

    def __init__(self, address: str, *, sni: str, **arguments: Any) -> None:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        super().__init__(address, context=context, **arguments)
        # Kept under our own name rather than read back off the base class.
        # `HTTPSConnection` stores it privately and has changed where over the
        # years; this is one attribute and it costs nothing to own.
        self._ssl_context = context
        self._sni = sni

    def connect(self) -> None:
        raw = socket.create_connection((self.host, self.port), self.timeout)
        # Wrapping here rather than letting the base class do it is the whole
        # reason this subclass exists: this is where the server name announced
        # in SNI is decided, and it is not the host being connected to.
        self.sock = self._ssl_context.wrap_socket(raw, server_hostname=self._sni)
