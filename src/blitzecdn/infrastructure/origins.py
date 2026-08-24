"""Describing an origin, and the controller's own narrow view of one.

Two jobs, and the split matters. ``to_probe`` renders a site's origin — host,
port, scheme, SNI — for whoever is going to connect to it, which for the
operator-facing check is the edges: ``EdgeOperationsService.check_origins``
runs a playbook, so the answer comes from the machines that actually carry the
traffic. This module used to *be* that check, and the answer it gave was about
the controller's network rather than the fleet's: an origin that allow-lists
the edges refuses the controller, and one reachable only from the controller's
subnet passed here and then 502'd on every edge.

``check`` is what remains of the controller connecting for itself, and it has
exactly one caller — the advisory origin check inside certificate preflight,
which answers during issuance and cannot wait for a playbook. It is advisory
there for precisely the reason above, and says so.

The hosts contacted are the origins an operator has already declared in
desired state, and the probe sends no application data — it connects,
completes a TLS handshake, and hangs up.
"""

from __future__ import annotations

import socket
import ssl
from collections.abc import Mapping
from contextlib import suppress
from typing import Any

from blitzecdn.config import Settings
from blitzecdn.domain.origins import OriginCheck
from blitzecdn.domain.sites import CdnSite, HttpScheme

_DEFAULT_PORTS = {HttpScheme.HTTP: 80, HttpScheme.HTTPS: 443}


class OriginProbe:
    """An origin's address, and what the controller alone can see of it."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def to_probe(self, site: CdnSite) -> dict[str, object]:
        """Render one site's origin for the edge-side check.

        The port and SNI are decided here rather than in the playbook so that
        the two probes — the edges' and the controller's advisory one — cannot
        disagree about what a site's origin is. A Jinja default in a role would
        be a second copy of this rule with no test holding it to the first.
        """
        return {
            "name": site.name,
            "origin_host": site.origin_host,
            "origin_port": site.origin_port
            or _DEFAULT_PORTS[site.ssl_mode.origin_scheme],
            "ssl_mode": site.ssl_mode.value,
            "origin_scheme": site.ssl_mode.origin_scheme.value,
            "origin_tls_verify": site.ssl_mode.verifies_origin,
            "origin_sni": site.effective_origin_sni,
        }

    def check(self, site: CdnSite) -> OriginCheck:
        scheme = site.ssl_mode.origin_scheme
        port = site.origin_port or _DEFAULT_PORTS[scheme]
        # The name the edge will put in the TLS handshake; probing with a
        # different one would verify a certificate the edge never asks for.
        sni = site.effective_origin_sni
        timeout = self._settings.origin_check_timeout_seconds
        result = OriginCheck(
            site=site.name,
            origin=f"{site.origin_host}:{port}",
            scheme=scheme,
            ssl_mode=site.ssl_mode,
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
                verify=site.ssl_mode.verifies_origin,
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
    """Mention the origin's own certificate expiry, which we do not manage.

    An origin certificate expiring takes the site down just as surely as one of
    ours, and nothing else in BlitzeCDN watches it.
    """
    if not peer or "notAfter" not in peer:
        return None
    return f"origin certificate valid until {peer['notAfter']}"
