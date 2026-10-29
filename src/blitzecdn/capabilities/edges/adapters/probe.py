"""Describing an origin, and the controller's own narrow view of one.

``to_probe`` renders an origin for the edges, whose network is what carries
traffic. ``check`` is the controller connecting itself, used only by the
advisory check in certificate preflight, which cannot wait for a play. It
connects, completes a TLS handshake, and hangs up.
"""

from __future__ import annotations

import socket
import ssl
from collections.abc import Mapping
from contextlib import suppress
from typing import Any

from blitzecdn.capabilities.edges.domain.origins import OriginCheck
from blitzecdn.capabilities.http.contract import DEFAULT_PORTS, HttpScheme
from blitzecdn.capabilities.serving.domain import CdnSite
from blitzecdn.capabilities.tls.contract import TlsPolicy
from blitzecdn.core.config import Settings


class OriginProbe:
    """An origin's address, and what the controller alone can see of it.

    Both probes address the canonical endpoint only — ``http://origin:80`` for Off
    and Flexible, ``https://origin:443`` for Full and Full (strict), the latter
    verified — not all thirteen alternate proxy ports. An origin closed on an
    alternate port 502s just for those requests. Flexible falls back to HTTPS on the
    alternate HTTPS ports, so it can 502 there despite passing; demanding TLS on
    five ports from every Flexible origin would be worse.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def to_probe(self, site: CdnSite) -> dict[str, object]:
        """Render one site's canonical origin for the edge-side check.

        Decided here, not in the playbook, so both probes agree on what the origin is.
        """
        return {
            "name": site.name,
            "origin_host": site.origin_host,
            "origin_port": DEFAULT_PORTS[site.canonical_origin_scheme],
            "ssl_mode": site.policy[TlsPolicy].ssl_mode.value,
            "origin_tls_verify": site.policy[TlsPolicy].ssl_mode.verifies_origin,
            "origin_sni": site.effective_origin_sni,
        }

    def check(self, site: CdnSite) -> OriginCheck:
        scheme = site.canonical_origin_scheme
        port = DEFAULT_PORTS[scheme]
        # The name the edge will put in the TLS handshake; probing with a
        # different one would verify a certificate the edge never asks for.
        sni = site.effective_origin_sni
        timeout = self._settings.origin_check_timeout_seconds
        result = OriginCheck(
            site=site.name,
            origin=f"{site.origin_host}:{port}",
            scheme=scheme,
            ssl_mode=site.policy[TlsPolicy].ssl_mode,
            sni=sni if scheme is HttpScheme.HTTPS else None,
        )

        try:
            addresses = socket.getaddrinfo(
                site.origin_host, port, proto=socket.IPPROTO_TCP
            )
        except socket.gaierror as exc:
            return result.model_copy(
                update={
                    "detail": (
                        f"{site.origin_host} does not resolve ({exc.strerror or exc}). "
                        "The edges resolve origins themselves, so this will fail "
                        "there too unless they use different DNS."
                    )
                }
            )
        if not addresses:
            return result.model_copy(
                update={"detail": f"{site.origin_host} resolves to no addresses"}
            )

        try:
            connection = socket.create_connection((site.origin_host, port), timeout)
        except TimeoutError:
            return result.model_copy(
                update={
                    "detail": (
                        f"no answer within {timeout}s. A firewall dropping the "
                        "packet looks exactly like this; check that the origin "
                        "admits connections from the edges."
                    )
                }
            )
        except OSError as exc:
            return result.model_copy(
                update={"detail": f"cannot connect: {exc.strerror or exc}"}
            )

        try:
            if scheme is HttpScheme.HTTP:
                return result.model_copy(update={"reachable": True})
            return self._handshake(
                result,
                connection,
                sni,
                timeout,
                verify=site.policy[TlsPolicy].ssl_mode.verifies_origin,
            )
        finally:
            with suppress(OSError):
                connection.close()

    @staticmethod
    def _handshake(
        result: OriginCheck,
        connection: socket.socket,
        sni: str,
        timeout: float,
        *,
        verify: bool,
    ) -> OriginCheck:
        """Complete the same verified or unverified handshake as the edge."""
        context = ssl.create_default_context()
        if not verify:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        connection.settimeout(timeout)
        try:
            with context.wrap_socket(connection, server_hostname=sni) as tls:
                peer = tls.getpeercert()
        except ssl.SSLCertVerificationError as exc:
            return result.model_copy(
                update={
                    "reachable": True,
                    "tls_verified": False,
                    "detail": (
                        f"connected, but the certificate is not valid for {sni!r}: "
                        f"{exc.verify_message or exc.reason}. Set origin_sni to the "
                        "name the origin actually presents, or fix the origin."
                    ),
                }
            )
        except (ssl.SSLError, OSError) as exc:
            return result.model_copy(
                update={
                    "reachable": True,
                    "tls_verified": False,
                    "detail": f"connected, but the TLS handshake failed: {exc}",
                }
            )
        return result.model_copy(
            update={
                "reachable": True,
                "tls_verified": True if verify else None,
                "detail": _expiry_note(peer) if verify else "TLS verification disabled",
            }
        )


def _expiry_note(peer: Mapping[str, Any] | None) -> str | None:
    """Mention the origin's own certificate expiry, which nothing else watches."""
    if not peer or "notAfter" not in peer:
        return None
    return f"origin certificate valid until {peer['notAfter']}"
