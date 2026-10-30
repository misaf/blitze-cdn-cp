"""Observe the expected release through the edge's public listener.

A reload or a response can come from the previous release, so a reserved
endpoint returns the digest rendered into the virtual host, whatever the
origin's health. TLS also checks the certificate names the hostname.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from collections.abc import Sequence
from typing import Any

from cryptography import x509

from blitzecdn.capabilities.http.contract import DEFAULT_PORTS, HttpScheme
from blitzecdn.capabilities.tls.contract import TlsPolicy
from blitzecdn.core.domain.site import CdnSite

__all__ = ["ServingProbe", "ServingResult"]

#: Reserved by the edge configuration, never forwarded to a customer's origin.
_PATH = "/.blitzecdn/release"

#: A response at all is the pass condition; this bounds the wait.
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

        A site's hostnames share one ``server`` block, so one name proves the rest.
        Sites with no hostnames are skipped.
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
        """One request to the edge's ``address``, naming ``hostname`` in Host and SNI.

        Not resolved through DNS, which may not point at this edge yet.
        """
        scheme = site.policy[TlsPolicy].canonical_visitor_scheme
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

    The chain is not verified: an uploaded private-CA certificate is supported
    and this controller may lack its trust root. The name is verified here, after
    the handshake, because ``check_hostname`` requires chain verification and
    ``CERT_NONE`` would accept another site's certificate. Expiry is left to
    renewal.
    """

    def __init__(self, address: str, *, sni: str, **arguments: Any) -> None:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        super().__init__(address, context=context, **arguments)
        # Kept under our own name: `HTTPSConnection` stores it privately.
        self._ssl_context = context
        self._sni = sni

    def connect(self) -> None:
        raw = socket.create_connection((self.host, self.port), self.timeout)
        # The reason for this subclass: SNI names the hostname, not the connected host.
        self.sock = self._ssl_context.wrap_socket(raw, server_hostname=self._sni)
        presented = self.sock.getpeercert(binary_form=True)
        if presented is None or not _certificate_covers(presented, self._sni):
            self.sock.close()
            raise ssl.SSLCertVerificationError(
                f"the edge presented a certificate that does not name {self._sni}"
            )


def _certificate_covers(der: bytes, hostname: str) -> bool:
    """Whether a DER certificate's SANs cover ``hostname``; CN alone does not count.

    A wildcard covers exactly one leftmost label.
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
