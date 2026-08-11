"""Probe the origins the edges will proxy to.

`ControlPlane.validate()` is static: it checks that desired state is internally
consistent and that Ansible can parse the playbook. It cannot catch the most
common way a new site fails in production — the origin is not reachable from
where we are, is listening on a different port, or presents a certificate that
does not match the SNI we will send. Those surface as 502s minutes after a
deploy that reported success.

This does the connection the edge would do, from the controller. That is not
the same vantage point as an edge, so a pass here is evidence rather than
proof; a failure, though, is nearly always real and is worth knowing before a
deploy rather than after.

The hosts contacted are the origins an operator has already declared in
desired state, and the probe sends no application data — it connects,
completes a TLS handshake, and hangs up.
"""

from __future__ import annotations

import socket
import ssl
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from typing import Any

from blitzecdn.config import Settings
from blitzecdn.domain.origins import OriginCheck
from blitzecdn.domain.sites import CdnSite, HttpScheme

_DEFAULT_PORTS = {HttpScheme.HTTP: 80, HttpScheme.HTTPS: 443}

#: Origins are probed in parallel because a fleet with many sites would
#: otherwise take (sites x timeout) seconds to report in the worst case, which
#: is long enough that nobody runs the check. Bounded so a large desired state
#: cannot open hundreds of sockets at once.
_MAX_PARALLEL_PROBES = 8


class OriginProbe:
    """Connects to an origin the way the edge will, and reports what happened."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def check_all(self, sites: list[CdnSite]) -> list[OriginCheck]:
        if not sites:
            return []
        workers = min(_MAX_PARALLEL_PROBES, len(sites))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(self.check, sites))

    def check(self, site: CdnSite) -> OriginCheck:
        port = site.origin_port or _DEFAULT_PORTS[site.origin_scheme]
        # The name the edge will put in the TLS handshake; probing with a
        # different one would verify a certificate the edge never asks for.
        sni = site.effective_origin_sni
        timeout = self._settings.origin_check_timeout_seconds
        result = OriginCheck(
            site=site.name,
            origin=f"{site.origin_host}:{port}",
            scheme=site.origin_scheme,
            sni=sni if site.origin_scheme is HttpScheme.HTTPS else None,
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
        result = result.model_copy(update={"resolved": True})

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
            if site.origin_scheme is HttpScheme.HTTP:
                return result.model_copy(update={"reachable": True})
            return self._handshake(result, connection, sni, timeout)
        finally:
            with suppress(OSError):
                connection.close()

    @staticmethod
    def _handshake(
        result: OriginCheck, connection: socket.socket, sni: str, timeout: float
    ) -> OriginCheck:
        """Complete the TLS handshake the edge would, verifying as it does.

        Verification is not optional here even though nginx's `proxy_ssl_verify`
        defaults to off. Reporting an unverified handshake as a pass would make
        this check agree with a deploy that later serves a certificate error to
        somebody, and the point of the check is to disagree first.
        """
        context = ssl.create_default_context()
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
                "tls_verified": True,
                "detail": _expiry_note(peer),
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
