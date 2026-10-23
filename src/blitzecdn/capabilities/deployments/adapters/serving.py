"""Observe the expected release through the edge's public listener.

A successful reload or an HTTP response can come from the previous release.
The reserved endpoint returns the digest embedded in the generated virtual
host, independently of origin health. A stale configuration therefore fails
verification even while serving otherwise healthy traffic. TLS still checks
that the certificate names the requested hostname.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from collections.abc import Sequence
from typing import Any

from cryptography import x509

from blitzecdn.capabilities.dns.domain import CdnSite
from blitzecdn.capabilities.http.policy import DEFAULT_PORTS, HttpScheme

__all__ = ["ServingProbe", "ServingResult"]

#: Reserved by the edge configuration, never forwarded to a customer's origin.
_PATH = "/.blitzecdn/release"

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
        self, *, edge: str, address: str, sites: Sequence[CdnSite], release_digest: str
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
        probes = sites or (
            CdnSite(
                name="default",
                server_names=("blitzecdn.invalid",),
                origin_host="127.0.0.1",
            ),
        )
        for site in probes:
            if not site.server_names:
                continue
            hostname = site.server_names[0]
            served, detail = self._request(address, hostname, site, release_digest)
            results.append(
                ServingResult(
                    edge=edge, hostname=hostname, served=served, detail=detail
                )
            )
        return tuple(results)

    def _request(
        self, address: str, hostname: str, site: CdnSite, release_digest: str
    ) -> tuple[bool, str]:
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
                observed = response.read(128).decode("ascii", errors="replace").strip()
            finally:
                connection.close()
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            return False, f"{type(exc).__name__}: {exc}"
        if response.status != 200 or observed != release_digest:
            return (
                False,
                f"expected release {release_digest}, observed {observed!r} "
                f"(HTTP {response.status})",
            )
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

    The chain is not verified. An edge may be serving a certificate this
    controller has no chain for; an uploaded certificate from a private CA is a
    supported mode, and verifying here would fail a correct deployment for a
    trust store this machine happens to lack.

    The *name* is verified, and by this class rather than by ``ssl``. This
    docstring used to say a certificate that does not match the SNI "still
    fails the handshake"; with ``CERT_NONE`` nothing in the handshake looks at
    the certificate at all, so an edge presenting another site's certificate —
    a swapped path, a stale file — passed as serving. ``check_hostname`` cannot
    be had without chain verification, so the names are read out of the
    certificate the edge sent and matched here, after the handshake.

    Expiry is not checked. Whether a deployment should fail for a certificate
    that is correctly installed but out of date is a policy question, and
    renewal is where it is answered.
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
        presented = self.sock.getpeercert(binary_form=True)
        if presented is None or not _certificate_covers(presented, self._sni):
            self.sock.close()
            raise ssl.SSLCertVerificationError(
                f"the edge presented a certificate that does not name {self._sni}"
            )


def _certificate_covers(der: bytes, hostname: str) -> bool:
    """Whether a DER certificate's subject alternative names cover ``hostname``.

    The SAN extension only, as every current client reads it: a certificate
    naming its host only in the subject CN is one a browser rejects, so it is
    not one this may accept. A wildcard covers exactly one leftmost label —
    ``*.example.com`` names ``cdn.example.com``, not ``example.com`` and not
    ``a.b.example.com`` — and a probe that asks for a wildcard ``server_name``
    itself is answered by that same wildcard in the certificate.
    """
    try:
        certificate = x509.load_der_x509_certificate(der)
        names = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
    except (ValueError, x509.ExtensionNotFound):
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        return address in names.get_values_for_type(x509.IPAddress)
    wanted = hostname.lower().rstrip(".")
    for name in names.get_values_for_type(x509.DNSName):
        candidate = name.lower().rstrip(".")
        if candidate == wanted:
            return True
        if candidate.startswith("*."):
            label, _, rest = wanted.partition(".")
            if label and label != "*" and rest == candidate[2:]:
                return True
    return False
